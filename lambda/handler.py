"""
Chennai Lake Watch - satellite water-area pipeline.

For each lake: search Earth Search (STAC) for new Sentinel-2 scenes, read only
the pixels inside the lake's bounding box from the public Cloud-Optimized
GeoTIFFs, compute NDWI water area, store it in DynamoDB, save a water-mask PNG
for the time-lapse, alert via SNS on sharp changes, and export data.json for
the website.

Event formats:
  {"list_lakes": true}                           returns lake ids (Step Functions)
  {"days": 10, "lakes": ["poondi"]}              scheduled run for some lakes
  {"backfill_days": 365, "lakes": ["poondi"]}    historical backfill, no alerts
  {"export_only": true}                          just rebuild data.json
Add "skip_export": true to process without rebuilding data.json (the Step
Functions map branches do this and export once at the end).
"""
import datetime as dt
import json
import os
import struct
import urllib.request
import zlib
from decimal import Decimal

import boto3
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import transform_bounds
from rasterio.windows import Window, from_bounds
from rasterio.windows import bounds as window_bounds

STAC_URL = "https://earth-search.aws.element84.com/v1/search"
COLLECTIONS = ["sentinel-2-c1-l2a", "sentinel-2-l2a"]

# Approximate bounding boxes [min_lon, min_lat, max_lon, max_lat].
# The website draws these on the map so you can check and fine-tune them:
# each box should enclose the lake at its fullest with a little margin, and
# as little other water (tanks, rivers) as possible.
LAKES = {
    "chembarambakkam": {"name": "Chembarambakkam Lake", "bbox": [80.025, 12.975, 80.095, 13.045]},
    "puzhal": {"name": "Puzhal (Red Hills) Lake", "bbox": [80.145, 13.135, 80.205, 13.195]},
    "poondi": {"name": "Poondi Reservoir", "bbox": [79.815, 13.155, 79.915, 13.245]},
    "sholavaram": {"name": "Sholavaram Lake", "bbox": [80.130, 13.215, 80.180, 13.260]},
    "pallikaranai": {"name": "Pallikaranai Marsh", "bbox": [80.190, 12.915, 80.240, 12.970]},
}

# Sentinel-2 scene classification (SCL) values: 0 no data, 3 cloud shadow,
# 8/9 cloud, 10 thin cirrus.
SCL_NODATA = 0
SCL_CLOUDY = [3, 8, 9, 10]
MAX_CLOUD_FRACTION = 0.10
MIN_VALID_FRACTION = 0.90

TABLE = os.environ.get("TABLE_NAME", "")
TOPIC_ARN = os.environ.get("TOPIC_ARN", "")
SITE_BUCKET = os.environ.get("SITE_BUCKET", "")
ALERT_PCT = float(os.environ.get("ALERT_CHANGE_PCT", "15"))

GDAL_ENV = dict(
    GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
    CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.TIF,.tiff",
    GDAL_HTTP_MAX_RETRY="3",
    GDAL_HTTP_RETRY_DELAY="1",
    VSI_CACHE="TRUE",
)

_dynamo = boto3.resource("dynamodb")
_s3 = boto3.client("s3")
_sns = boto3.client("sns")


# ---------------------------------------------------------------- STAC search
def _http_json(url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "chennai-lake-watch"},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def stac_search(collection, bbox, start, end):
    body = {
        "collections": [collection],
        "bbox": bbox,
        "datetime": f"{start}T00:00:00Z/{end}T23:59:59Z",
        "limit": 100,
        "query": {"eo:cloud_cover": {"lt": 60}},
    }
    features, url, payload = [], STAC_URL, body
    for _ in range(20):  # page limit
        page = _http_json(url, payload)
        features += page.get("features", [])
        nxt = next((l for l in page.get("links", []) if l.get("rel") == "next"), None)
        if not nxt:
            break
        url = nxt["href"]
        if nxt.get("method", "GET").upper() == "POST":
            payload = {**body, **nxt.get("body", {})} if nxt.get("merge") else nxt.get("body", body)
        else:
            payload = None
    return features


def _contains(outer, inner):
    return outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]


def find_scenes(bbox, start, end):
    """One scene per date whose tile fully covers the lake, oldest first."""
    by_date = {}
    for collection in COLLECTIONS:
        try:
            feats = stac_search(collection, bbox, start, end)
        except Exception as exc:  # collection missing or API hiccup
            print(f"search failed for {collection}: {exc}")
            continue
        for f in feats:
            date = f["properties"]["datetime"][:10]
            assets = f.get("assets", {})
            if date in by_date or not all(k in assets for k in ("green", "nir", "scl")):
                continue
            if f.get("bbox") and not _contains(f["bbox"], bbox):
                continue
            by_date[date] = f
    return sorted(by_date.items())


# ---------------------------------------------------------------- raster math
def _href(asset):
    href = asset["href"]
    if href.startswith("s3://"):
        bucket, key = href[5:].split("/", 1)
        href = f"https://{bucket}.s3.us-west-2.amazonaws.com/{key}"
    return href


def read_window(href, bbox, out_shape=None):
    with rasterio.open(href) as src:
        b = transform_bounds("EPSG:4326", src.crs, *bbox, densify_pts=21)
        w = from_bounds(*b, transform=src.transform)
        win = Window(int(np.floor(w.col_off)), int(np.floor(w.row_off)),
                     int(np.ceil(w.width)), int(np.ceil(w.height)))
        kwargs = dict(window=win, boundless=True, fill_value=0)
        if out_shape is not None:
            kwargs.update(out_shape=out_shape, resampling=Resampling.nearest)
        arr = src.read(1, **kwargs)
        pixel_area = abs(src.res[0] * src.res[1])
        # Where this pixel window sits on the map, for the website overlay.
        geo = transform_bounds(src.crs, "EPSG:4326", *window_bounds(win, src.transform))
    return arr, pixel_area, [round(v, 6) for v in geo]


def analyse(item, bbox):
    a = item["assets"]
    with rasterio.Env(**GDAL_ENV):
        green, px_area, geo_bounds = read_window(_href(a["green"]), bbox)
        nir, _, _ = read_window(_href(a["nir"]), bbox, out_shape=green.shape)
        # SCL is 20 m; reading it into the 10 m grid upsamples it.
        scl, _, _ = read_window(_href(a["scl"]), bbox, out_shape=green.shape)

    g = green.astype("float32")
    n = nir.astype("float32")
    valid = (scl != SCL_NODATA) & (g > 0) & (n > 0)
    if valid.mean() < MIN_VALID_FRACTION:
        return None, "missing data over lake"
    cloud = np.isin(scl, SCL_CLOUDY) & valid
    cloud_frac = cloud.sum() / max(valid.sum(), 1)
    if cloud_frac > MAX_CLOUD_FRACTION:
        return None, f"{cloud_frac:.0%} cloud over lake"

    clear = valid & ~cloud
    # NDWI = (green - nir) / (green + nir); > 0 means open water.
    # A constant radiometric offset on both bands doesn't change the sign,
    # so the threshold at 0 works for both processing baselines.
    ndwi = (g - n) / np.maximum(g + n, 1.0)
    water = clear & (ndwi > 0)
    result = {
        "water_km2": float(water.sum() * px_area / 1e6),
        "cloud_pct": float(cloud_frac * 100),
        "scene_id": item["id"],
        "mask": water_mask_png(water, cloud),
        "mask_bounds": geo_bounds,
    }
    return result, "ok"


def water_mask_png(water, cloud):
    """RGBA PNG: blue water, grey cloud, transparent land. No Pillow needed."""
    h, w = water.shape
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    rgba[water] = (37, 99, 235, 210)
    rgba[cloud] = (160, 160, 160, 150)
    raw = np.concatenate([np.zeros((h, 1), np.uint8), rgba.reshape(h, w * 4)], axis=1).tobytes()

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


# ---------------------------------------------------------------- storage
def already_done(table, lake_id, date):
    return "Item" in table.get_item(Key={"lake": lake_id, "date": date})


def previous_record(table, lake_id, date):
    resp = table.query(
        KeyConditionExpression="lake = :l AND #d < :d",
        ExpressionAttributeNames={"#d": "date"},
        ExpressionAttributeValues={":l": lake_id, ":d": date},
        ScanIndexForward=False,
        Limit=1,
    )
    items = resp.get("Items", [])
    return items[0] if items else None


def save(table, lake_id, date, res):
    key = f"masks/{lake_id}/{date}.png"
    _s3.put_object(Bucket=SITE_BUCKET, Key=key, Body=res["mask"],
                   ContentType="image/png", CacheControl="max-age=86400")
    table.put_item(Item={
        "lake": lake_id,
        "date": date,
        "water_km2": Decimal(str(round(res["water_km2"], 4))),
        "cloud_pct": Decimal(str(round(res["cloud_pct"], 2))),
        "scene_id": res["scene_id"],
        "mask": key,
        "mask_bounds": [Decimal(str(v)) for v in res["mask_bounds"]],
    })


def maybe_alert(lake_id, date, area, prev):
    if not prev or not TOPIC_ARN:
        return
    before = float(prev["water_km2"])
    if before <= 0:
        return
    change = (area - before) / before * 100
    if abs(change) >= ALERT_PCT:
        name = LAKES[lake_id]["name"]
        direction = "rose" if change > 0 else "fell"
        _sns.publish(
            TopicArn=TOPIC_ARN,
            Subject=f"Lake Watch: {name} water area {direction} {abs(change):.0f}%"[:99],
            Message=(f"{name} water area {direction} from {before:.2f} km2 on {prev['date']} "
                     f"to {area:.2f} km2 on {date} ({change:+.1f}%)."),
        )


def export(table):
    items, kwargs = [], {}
    while True:
        resp = table.scan(**kwargs)
        items += resp.get("Items", [])
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    lakes = []
    for lake_id, meta in LAKES.items():
        series = sorted(
            ({"date": i["date"], "water_km2": float(i["water_km2"]),
              "cloud_pct": float(i["cloud_pct"]), "mask": i["mask"],
              "mask_bounds": [float(v) for v in i.get("mask_bounds", meta["bbox"])]}
             for i in items if i["lake"] == lake_id),
            key=lambda r: r["date"],
        )
        lakes.append({"id": lake_id, "name": meta["name"], "bbox": meta["bbox"], "series": series})

    doc = {"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "lakes": lakes}
    _s3.put_object(Bucket=SITE_BUCKET, Key="data.json", Body=json.dumps(doc).encode(),
                   ContentType="application/json", CacheControl="max-age=300")
    return sum(len(l["series"]) for l in lakes)


# ---------------------------------------------------------------- entry point
def lambda_handler(event, context):
    event = event or {}
    # Step Functions passes the execution input under "options".
    event = {**(event.get("options") or {}), **{k: v for k, v in event.items() if k != "options"}}
    if event.get("list_lakes"):
        return {"lakes": [l for l in (event.get("lakes") or LAKES) if l in LAKES]}
    table = _dynamo.Table(TABLE)
    if event.get("export_only"):
        return {"exported_records": export(table)}

    backfill = "backfill_days" in event
    days = int(event.get("backfill_days") or event.get("days") or 10)
    lake_ids = [l for l in (event.get("lakes") or LAKES) if l in LAKES]
    end = dt.datetime.now(dt.timezone.utc).date()
    start = end - dt.timedelta(days=days)

    log, out_of_time = [], False
    for lake_id in lake_ids:
        bbox = LAKES[lake_id]["bbox"]
        for date, item in find_scenes(bbox, start, end):
            if context and context.get_remaining_time_in_millis() < 90_000:
                out_of_time = True
                break
            if already_done(table, lake_id, date):
                continue
            try:
                res, why = analyse(item, bbox)
            except Exception as exc:
                res, why = None, f"error: {exc}"
            if res is None:
                log.append(f"{lake_id} {date}: skipped ({why})")
                continue
            prev = None if backfill else previous_record(table, lake_id, date)
            save(table, lake_id, date, res)
            maybe_alert(lake_id, date, res["water_km2"], prev)
            log.append(f"{lake_id} {date}: {res['water_km2']:.2f} km2")
        if out_of_time:
            break

    print("\n".join(log) or "no new scenes")
    if event.get("skip_export"):
        return {"processed": log, "out_of_time": out_of_time}
    return {"processed": log, "exported_records": export(table), "out_of_time": out_of_time}
