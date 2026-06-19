#!/bin/bash

# Configuration
BASE_DIR="/opt/web"
DAYS=60   # <-- Change this to the number of days you want

TARGET_DIRS=(
  "gop-results"
  "ingest-results"
)

# Parse flags
AUTO_CONFIRM=false
while getopts "y" opt; do
  case ${opt} in
    y )
      AUTO_CONFIRM=true
      ;;
    \? )
      echo "Usage: $0 [-y]"
      exit 1
      ;;
  esac
done

# Execution
for dir in "${TARGET_DIRS[@]}"; do
  FULL_PATH="$BASE_DIR/$dir"

  if [ -d "$FULL_PATH" ]; then
    
    if [ "$AUTO_CONFIRM" = true ]; then
      echo "Auto-confirm mode (-y) active. Cleaning $FULL_PATH (removing *.ts and *.zip files older than $DAYS days)..."
      # Run direct deletion in auto mode
      find "$FULL_PATH" -type f \( -name "*.ts" -o -name "*.zip" \) -mtime +$DAYS -print -delete
    else
      echo "Analyzing $FULL_PATH (files older than $DAYS days)..."
      
      # List files first for preview
      FILES_TO_DELETE=$(find "$FULL_PATH" -type f \( -name "*.ts" -o -name "*.zip" \) -mtime +$DAYS)

      if [ -z "$FILES_TO_DELETE" ]; then
        echo "No files found to delete in $FULL_PATH."
        echo "----------------------------------------"
        continue
      fi

      echo "The following files will be deleted:"
      echo "$FILES_TO_DELETE"
      echo "----------------------------------------"
      
      # Request manual confirmation
      read -p "Do you really want to delete these files? [y/N]: " CONFIRM
      
      # Convert response to lowercase to accept Y or y
      if [[ "${CONFIRM,,}" =~ ^(y|yes)$ ]]; then
        echo "Deleting files..."
        # Safely delete the found files
        echo "$FILES_TO_DELETE" | xargs -d '\n' rm -f
        echo "Cleanup completed for $FULL_PATH."
      else
        echo "Operation cancelled by the user for $FULL_PATH."
      fi
    fi

  else
    echo "Directory not found: $FULL_PATH"
  fi
  echo "----------------------------------------"
done