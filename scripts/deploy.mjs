#!/usr/bin/env node
// Deploy Home On The Range (viewer + committed data) to its own S3 bucket,
// mirroring bigolbuffalo's deploy. The site is served through CloudFront;
// bigolbuffalo.com/HomeOnTheRange.html iframes it.
//
// Only the viewer and the five data dirs it reads are synced — data/census
// holds ~850 MB of raw pulls the viewer never touches.
import { execSync } from "node:child_process";
import { writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const BUCKET = "bigolbuffalo-homeontherange";
const DISTRIBUTION_ID = "E1TMINH1ED64R9";
const SITE_URL = "https://d3r7y13krtzdja.cloudfront.net/viewer/";

function step(label, cmd) {
  console.log(`\n--- ${label} ---`);
  console.log(`$ ${cmd}\n`);
  execSync(cmd, { stdio: "inherit" });
}

step("sync viewer", `aws s3 sync viewer/ s3://${BUCKET}/viewer/ --delete`);

// Boundaries: .geojson would sync as octet-stream, which CloudFront won't
// compress — force application/json (18 MB of polygons wants gzip).
step("sync boundaries (meta)",
  `aws s3 sync data/boundaries/ s3://${BUCKET}/data/boundaries/ --delete ` +
  `--exclude "_snapshots/*" --exclude "*.geojson" --exclude "*.REVIEW"`);
step("sync boundaries (geojson)",
  `aws s3 sync data/boundaries/ s3://${BUCKET}/data/boundaries/ ` +
  `--exclude "*" --include "*.geojson" --exclude "_snapshots/*" ` +
  `--content-type application/json`);

for (const dir of ["cad", "joined", "schools", "derived"]) {
  step(`sync ${dir}`,
    `aws s3 sync data/${dir}/ s3://${BUCKET}/data/${dir}/ --delete`);
}

// Root of the distribution redirects to the viewer.
const rootFile = join(tmpdir(), "hotr-root-index.html");
writeFileSync(rootFile,
  '<!doctype html><meta http-equiv="refresh" content="0; url=/viewer/">' +
  '<a href="/viewer/">Home On The Range</a>\n');
step("root redirect",
  `aws s3 cp "${rootFile}" s3://${BUCKET}/index.html --content-type text/html`);
rmSync(rootFile, { force: true });

step("invalidate CloudFront cache",
  `aws cloudfront create-invalidation --distribution-id ${DISTRIBUTION_ID} ` +
  `--paths "/*" --query "Invalidation.{Id:Id,Status:Status}" --output table`);

console.log(`\nDeployed. ${SITE_URL}`);
console.log("CloudFront invalidation takes 1-5 min to fully propagate.");
