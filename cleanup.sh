#!/bin/bash

# Configuration
BASE_DIR="/opt/web"
DAYS=60   # <-- Change this to the number of days you want

TARGET_DIRS=(
  "gop-results"
  "ingest-results"
)

# Execution
for dir in "${TARGET_DIRS[@]}"; do
  FULL_PATH="$BASE_DIR/$dir"

  if [ -d "$FULL_PATH" ]; then
    echo "Cleaning $FULL_PATH (removing *.ts and *.zip files older than $DAYS days)..."

    find "$FULL_PATH" -type f \( -name "*.ts" -o -name "*.zip" \) -mtime +$DAYS -print -delete

  else
    echo "Directory not found: $FULL_PATH"
  fi
done