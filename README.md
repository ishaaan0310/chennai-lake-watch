# Chennai Lake Watch

**Tracking the water in Chennai's lakes from space**, using free Sentinel-2 satellite imagery and a fully serverless AWS pipeline.

Every few days a Sentinel-2 satellite passes over Chennai. This project automatically finds each new image, measures how much of each lake is water, stores the history, emails an alert when a lake changes sharply, and publishes an interactive map with a time-lapse and trend chart.

Lakes tracked: **Chembarambakkam, Puzhal (Red Hills), Poondi, Sholavaram and Pallikaranai Marsh.**

**Live demo:** _add your CloudFront link here_ · **Demo video:** _add link_

---

## Architecture

```
EventBridge Scheduler (daily, 9:00 IST)
        │
        ▼
Step Functions ── ListLakes ──► Map (one branch per lake, in parallel) ──► PublishWebsiteData
                                      │
                                      ▼
                           Lambda (Python, rasterio, NumPy)
                           1. Earth Search STAC: find new scenes over the lake
                           2. Read only the lake's pixels from Sentinel-2 COGs
                              (public S3 bucket, AWS Open Data, us-west-2)
                           3. Mask clouds (SCL band), compute NDWI, count water
                                      │
             ┌────────────────────────┼─────────────────────────┐
             ▼                        ▼                         ▼
        DynamoDB                 S3 (water-map PNGs,        SNS email alert
   (lake, date) → km²            data.json for the site)    on sharp change
                                      │
                                      ▼
                        CloudFront → website (Leaflet map + Chart.js)

CloudWatch alarm on failed runs → SNS  ·  AWS Budgets email at $5/month
```

| AWS service | Role |
|---|---|
| AWS Open Data (Sentinel-2 COGs) | Free satellite imagery, read in place |
| EventBridge Scheduler | Daily trigger, in the Asia/Kolkata timezone |
| Step Functions | Orchestration, parallel Map per lake, retries |
| Lambda | Scene search, NDWI water measurement, PNG rendering |
| DynamoDB | Time series of water area per lake |
| S3 | Website, water-map images, data file |
| CloudFront (+ Origin Access Control) | HTTPS website, private bucket |
| SNS | Lake-change alerts and failure alarms by email |
| CloudWatch | Logs (14-day retention) and a failure alarm |
| AWS Budgets | Cost guardrail |
| CloudFormation / SAM | Everything above is infrastructure as code |

### Key design decisions

- **Region us-west-2.** The imagery lives in Oregon. Running compute next to the data makes reads fast and free of data-transfer charges, even though the lakes are in Chennai.
- **Read only what's needed.** Sentinel-2 tiles are 110 × 110 km. Because they're stored as Cloud-Optimized GeoTIFFs, the Lambda uses HTTP range requests to read just the few hundred kilobytes covering each lake.
- **Water index.** NDWI = (Green − NIR) / (Green + NIR); pixels above 0 are water. The newer Sentinel-2 processing adds a constant offset of 1000 to every band, which doesn't change the sign of NDWI, so the same threshold works across the whole archive.
- **Clouds.** Chennai's monsoon means many cloudy scenes. Each scene's Scene Classification Layer marks clouds and shadows; scenes with more than 10% cloud over a lake are skipped rather than reported as "less water".
- **Idempotent.** Each (lake, date) is processed once; re-running a backfill only fills gaps.
- **No servers, no VPC, no NAT Gateway**, so the whole project runs within the AWS free plan.

---

## Deploy in 60 minutes

Everything runs in **AWS CloudShell** (a terminal in your browser, already signed in to your account), so you don't need to install anything.

| Time | Step |
|---|---|
| 0–5 min | Open the AWS Console, switch region to **US West (Oregon) us-west-2**, open **CloudShell** (the `>_` icon at the top). |
| 5–10 min | Get the code into CloudShell: `git clone https://github.com/<you>/chennai-lake-watch.git && cd chennai-lake-watch` |
| 10–25 min | Deploy: `./deploy.sh you@example.com --backfill` (builds the Lambda package, creates every resource, uploads the website, starts a one-year history backfill). CloudFront takes most of this time. |
| 25–27 min | Check your email and click **Confirm subscription** for the alerts. |
| 27–45 min | While the backfill runs, open the Step Functions link printed by the script and watch the five lake branches run in parallel. Open the website link. |
| 45–55 min | Check the dashed lake outlines on the map. If one doesn't enclose its lake, edit its `bbox` in `lambda/handler.py` and re-run `./deploy.sh you@example.com`. |
| 55–60 min | Take screenshots, add your live link to this README, push to GitHub. |

### Useful commands

```bash
# Run the workflow now (checks the last 10 days)
aws stepfunctions start-execution --region us-west-2 \
  --state-machine-arn <StateMachineArn from the deploy output> --input '{"days": 10}'

# Backfill more history, for one lake (e.g. two years)
aws stepfunctions start-execution --region us-west-2 \
  --state-machine-arn <StateMachineArn> --input '{"backfill_days": 730, "lakes": ["chembarambakkam"]}'

# Follow the Lambda logs
aws logs tail /aws/lambda/chennai-lake-watch-pipeline --region us-west-2 --follow

# Delete everything
./destroy.sh
```

A long backfill may stop early to stay within Lambda's 15-minute limit (`"out_of_time": true` in the output). Just run it again; it continues where it left off.

---

## Cost

Designed for the AWS free plan. Lambda, DynamoDB (provisioned at 5 units), Step Functions, SNS and CloudWatch stay within their always-free allowances at this scale; reading the open imagery in the same region is free. S3 and CloudFront usage for a portfolio site is a few cents at most. A $5 monthly budget alert is created automatically.

## Project structure

```
lambda/handler.py        pipeline: scene search, NDWI, storage, alerts, export
lambda/requirements.txt  rasterio, numpy
template.yaml            all infrastructure (AWS SAM / CloudFormation)
website/index.html       map, time-lapse slider, trend chart
deploy.sh / destroy.sh   one-command deploy and teardown (AWS CloudShell)
```

## Ideas for next steps

- Before/after view of Cyclone Michaung (December 2023) flooding.
- Sentinel-1 radar imagery, which sees through monsoon clouds.
- Precise lake outlines from OpenStreetMap instead of bounding boxes.
- GitHub Actions pipeline that deploys on every push.

## Data

Contains modified Copernicus Sentinel data, accessed through the [AWS Registry of Open Data](https://registry.opendata.aws/sentinel-2-l2a-cogs/) and [Element 84 Earth Search](https://github.com/Element84/earth-search). Water areas are estimates from 10 m pixels.
