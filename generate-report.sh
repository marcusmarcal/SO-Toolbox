#!/bin/bash

#set -x

# generate-report.sh - Creates HTML reports from SRT stream analysis data
# Usage: ./generate-report.sh -t template.html -j report.json -o output_dir

VERSION="1.1.1"
if [[ "${1:-}" == "--version" ]]; then
    echo "ingest-analyzer $VERSION"
    exit 0
fi

# Default values
TEMPLATE_FILE=""
OUTPUT_DIR="./report"
TITLE="Ingest Analysis Report"
JSON_FILE=""
CHART_PATH="."

# Parse command line options
while [[ $# -gt 0 ]]; do
  case $1 in
    -h|--help)
      echo "Usage: $0 -t template.html -j report.json -o output_dir"
      echo "Options:"
      echo "  -h, --help                Display this help message"
      echo "  -t, --template FILE       Use HTML template file (required)"
      echo "  -o, --output-dir DIR      Output directory for the report"
      echo "  -j, --json-file FILE      Original JSON file analyzed (required)"
      echo "  -c, --chart-path PATH     Path to chart files (default: .)"
      exit 0
      ;;
    -t|--template)
      TEMPLATE_FILE="$2"
      shift 2
      ;;
    -o|--output-dir)
      OUTPUT_DIR="$2"
      shift 2
      ;;
    -j|--json-file)
      JSON_FILE="$2"
      shift 2
      ;;
    -c|--chart-path)
      CHART_PATH="$2"
      shift 2
      ;;
    *)
      echo "Unknown option: $1"
      exit 1
      ;;
  esac
done

# Create output directory if it doesn't exist
mkdir -p "$OUTPUT_DIR"

# Check for required template and JSON files
if [[ -z "$TEMPLATE_FILE" || ! -f "$TEMPLATE_FILE" ]]; then
  echo "Error: Template file is required and must exist"
  echo "Usage: $0 -t template.html -j report.json -o output_dir"
  exit 1
fi

if [[ -z "$JSON_FILE" || ! -f "$JSON_FILE" ]]; then
  echo "Error: JSON file is required and must exist"
  echo "Usage: $0 -t template.html -j report.json -o output_dir"
  exit 1
fi

# Get current timestamp for the footer
CURRENT_TIMESTAMP=$(date -u +"%Y-%m-%d %H:%M:%S UTC")

format_number() {
    local num="$1"

    # If not a valid number, return as-is
    [[ ! "$num" =~ ^[0-9]+$ ]] && echo "$num" && return

    # Reverse number, add commas every 3 digits, then reverse again
    echo "$num" | rev | sed -E 's/([0-9]{3})/\1,/g' | rev | sed -E 's/^,//'
}

# Read values from JSON file
INPUT_ARG=$(jq -r '.input_arg // empty' "$JSON_FILE")

# Remove passphrase from URL for display (security — never show passphrase in reports)
INPUT_ARG_CLEAN=$(echo "$INPUT_ARG" | sed 's/[?&]passphrase=[^&]*//' | sed 's/[?&]$//')

if [[ "$INPUT_ARG_CLEAN" != rtmp://* ]]; then
  INPUT_ARG_SHORT=$(basename "$INPUT_ARG_CLEAN")
else
  INPUT_ARG_SHORT="$INPUT_ARG_CLEAN"
fi

TITLE=$(jq -r ".title // \"Ingest Analysis: $INPUT_ARG_CLEAN\"" "$JSON_FILE")
TIMESTAMP=$(jq -r '.timestamp' "$JSON_FILE")

# Extract metrics from the report file
VIDEO_CODEC=$(jq -r '.video.codec' "$JSON_FILE")
VIDEO_PROFILE=$(jq -r '.video.profile' "$JSON_FILE")
VIDEO_BITRATE=$(format_number "$(jq -r '.video.bitrate' "$JSON_FILE")")
VIDEO_PIXEL_FORMAT=$(jq -r '.video.pixel_format' "$JSON_FILE")
VIDEO_TRACK_COUNT=$(jq -r '.video.track_count' "$JSON_FILE")
VIDEO_R_FRAME_RATE=$(jq -r '.video.r_frame_rate' "$JSON_FILE")
VIDEO_RESOLUTION=$(jq -r '.video.resolution' "$JSON_FILE")
VIDEO_HAS_B_FRAMES=$(jq -r '.video.has_b_frames' "$JSON_FILE")

# Convert video and audio bitrates to more readable units
if [[ -n "$VIDEO_BITRATE" && "$VIDEO_BITRATE" != "unknown" ]]; then
    VIDEO_BITRATE_MBPS=$(echo "scale=2; ${VIDEO_BITRATE//,/} / 1000000" | bc)
else
    VIDEO_BITRATE_MBPS="N/A"
fi

AUDIO_CODEC=$(jq -r '.audio.codec' "$JSON_FILE")
AUDIO_PROFILE=$(jq -r '.audio.profile' "$JSON_FILE")
AUDIO_SAMPLE_RATE=$(format_number "$(jq -r '.audio.sample_rate' "$JSON_FILE")")
AUDIO_BITRATE=$(format_number "$(jq -r '.audio.bitrate' "$JSON_FILE")")
AUDIO_TRACK_COUNT=$(jq -r '.audio.track_count' "$JSON_FILE")

if [[ -n "$AUDIO_BITRATE" && "$AUDIO_BITRATE" != "unknown" ]]; then
    AUDIO_BITRATE_KBPS=$(format_number "$(echo "scale=2; ${AUDIO_BITRATE//,/} / 1000" | bc)")
else
    AUDIO_BITRATE_KBPS="N/A"
fi

MIN_AV_SYNC=$(jq -r '.av_sync.min' "$JSON_FILE")
MAX_AV_SYNC=$(jq -r '.av_sync.max' "$JSON_FILE")
AVG_AV_SYNC=$(jq -r '.av_sync.avg' "$JSON_FILE")
MEDIAN_AV_SYNC=$(jq -r '.av_sync.median' "$JSON_FILE")

# Extract PTS jitter values
VIDEO_PTS_JITTER=$(jq -r '.pts_jitter.video_avg // "N/A"' "$JSON_FILE")
AUDIO_PTS_JITTER=$(jq -r '.pts_jitter.audio_avg // "N/A"' "$JSON_FILE")

# Extract test results (PASS/FAIL/WARN counts)
TOTAL_TESTS=$(jq -r '.test_results.summary.total' "$JSON_FILE")
PASSED_TESTS=$(jq -r '.test_results.summary.passed' "$JSON_FILE")
FAILED_TESTS=$(jq -r '.test_results.summary.failed' "$JSON_FILE")
WARNED_TESTS=$(jq -r '.test_results.summary.warned' "$JSON_FILE")

# Temporary file for the rendered HTML
TEMP_HTML=$(mktemp)

# Read template file
TEMPLATE=$(cat "$TEMPLATE_FILE")

# Simple template variable replacement function
function replace_template_var() {
  local template="$1"
  local var_name="$2"
  local var_value="$3"

  # Escape special characters in the value to prevent issues with sed
  var_value=$(echo "$var_value" | sed 's/[\/&]/\\&/g')

  # Replace {{VAR_NAME}} with var_value
  echo "$template" | sed "s/{{$var_name}}/$var_value/g"
}

# Process conditional sections with {{#SECTION_NAME}}...{{/SECTION_NAME}} format
function process_conditional_section() {
  local template="$1"
  local section_name="$2"
  local include_section="$3"  # true or false

  if [[ "$include_section" == "true" ]]; then
    # Remove section markers but keep the content
    echo "$template" | sed "s|{{#$section_name}}||g" | sed "s|{{/$section_name}}||g"
  else
    # Remove the entire section including content
    echo "$template" | perl -g -pe "s|{{#$section_name}}.*?{{/$section_name}}||gs"
  fi
}

# Start with the template
RESULT="$TEMPLATE"

# Replace basic variables
RESULT=$(replace_template_var "$RESULT" "TITLE" "$TITLE")
RESULT=$(replace_template_var "$RESULT" "CURRENT_TIMESTAMP" "$CURRENT_TIMESTAMP")
RESULT=$(replace_template_var "$RESULT" "JSON_FILENAME" "$(basename $JSON_FILE)")
RESULT=$(replace_template_var "$RESULT" "VIDEO_CODEC" "$VIDEO_CODEC")
RESULT=$(replace_template_var "$RESULT" "VIDEO_PROFILE" "$VIDEO_PROFILE")
RESULT=$(replace_template_var "$RESULT" "VIDEO_PIXEL_FORMAT" "$VIDEO_PIXEL_FORMAT")
RESULT=$(replace_template_var "$RESULT" "VIDEO_BITRATE" "$VIDEO_BITRATE_MBPS")
RESULT=$(replace_template_var "$RESULT" "VIDEO_TRACK_COUNT" "$VIDEO_TRACK_COUNT")
RESULT=$(replace_template_var "$RESULT" "VIDEO_R_FRAME_RATE" "$VIDEO_R_FRAME_RATE")
RESULT=$(replace_template_var "$RESULT" "VIDEO_RESOLUTION" "$VIDEO_RESOLUTION")
RESULT=$(replace_template_var "$RESULT" "VIDEO_HAS_B_FRAMES" "$VIDEO_HAS_B_FRAMES")
RESULT=$(replace_template_var "$RESULT" "AUDIO_CODEC" "$AUDIO_CODEC")
RESULT=$(replace_template_var "$RESULT" "AUDIO_PROFILE" "$AUDIO_PROFILE")
RESULT=$(replace_template_var "$RESULT" "AUDIO_SAMPLE_RATE" "$AUDIO_SAMPLE_RATE")
RESULT=$(replace_template_var "$RESULT" "AUDIO_BITRATE" "$AUDIO_BITRATE_KBPS")
RESULT=$(replace_template_var "$RESULT" "AUDIO_TRACK_COUNT" "$AUDIO_TRACK_COUNT")
RESULT=$(replace_template_var "$RESULT" "MIN_AV_SYNC" "$MIN_AV_SYNC")
RESULT=$(replace_template_var "$RESULT" "MAX_AV_SYNC" "$MAX_AV_SYNC")
RESULT=$(replace_template_var "$RESULT" "AVG_AV_SYNC" "$AVG_AV_SYNC")
RESULT=$(replace_template_var "$RESULT" "MEDIAN_AV_SYNC" "$MEDIAN_AV_SYNC")
RESULT=$(replace_template_var "$RESULT" "VIDEO_PTS_JITTER" "$VIDEO_PTS_JITTER")
RESULT=$(replace_template_var "$RESULT" "AUDIO_PTS_JITTER" "$AUDIO_PTS_JITTER")
RESULT=$(replace_template_var "$RESULT" "TOTAL_TESTS" "$TOTAL_TESTS")
RESULT=$(replace_template_var "$RESULT" "PASSED_TESTS" "$PASSED_TESTS")
RESULT=$(replace_template_var "$RESULT" "FAILED_TESTS" "$FAILED_TESTS")
RESULT=$(replace_template_var "$RESULT" "WARNED_TESTS" "$WARNED_TESTS")

# Process conditional sections
# Include AV Sync Section if any of the AV sync metrics are provided
if [[ -n "$MIN_AV_SYNC" && -n "$MAX_AV_SYNC" && -n "$AVG_AV_SYNC" && -n "$MEDIAN_AV_SYNC" ]]; then
  RESULT=$(process_conditional_section "$RESULT" "AV_SYNC_SECTION" "true")
else
  RESULT=$(process_conditional_section "$RESULT" "AV_SYNC_SECTION" "false")
fi

# Include test results section if test data is provided
if [[ -n "$TOTAL_TESTS" && -n "$PASSED_TESTS" && -n "$FAILED_TESTS" && -n "$WARNED_TESTS" ]]; then
  RESULT=$(process_conditional_section "$RESULT" "TEST_RESULTS_SECTION" "true")
else
  RESULT=$(process_conditional_section "$RESULT" "TEST_RESULTS_SECTION" "false")
fi

# Read chart information from the JSON file
CHART_FILES=()
CHART_TITLES=()

if [[ -f "$JSON_FILE" ]]; then
  # Extract chart file paths and titles from the "charts" array in the JSON file
  CHART_FILES=($(jq -r '.charts[] | .file' "$JSON_FILE"))
  while IFS= read -r title; do
    CHART_TITLES+=("$title")
  done < <(jq -r '.charts[] | .title' "$JSON_FILE")
else
  echo "Error: JSON file not found or invalid. Cannot populate charts."
  exit 1
fi

# Generate charts HTML
CHARTS_HTML=""
for i in "${!CHART_FILES[@]}"; do
  CHARTS_HTML+="        <div class=\"chart-container\">
            <div class=\"chart-header\">${CHART_TITLES[$i]}</div>
            <div class=\"chart-body\">
                <img src=\"${CHART_FILES[$i]}\" alt=\"${CHART_TITLES[$i]}\" class=\"chart-img\">
            </div>
        </div>
"
done

# Replace charts section in the HTML template
RESULT=$(echo "$RESULT" | perl -g -pe "s|{{#CHARTS_SECTION}}.*?{{/CHARTS_SECTION}}|$CHARTS_HTML|gs")

# Example test results (replace with actual data)
TEST_RESULTS_JSON="$OUTPUT_DIR/report.json"
TEST_RESULTS=$(jq -r '.test_results.details[] | "\(.description)|\(.comparison)|\(.desired)|\(.actual)|\(.units)|\(.result)|\(.notes)"' "$TEST_RESULTS_JSON")

# Generate test results table HTML
TEST_RESULTS_TABLE=""
TEST_COUNT=$(jq '.test_results.details | length' "$TEST_RESULTS_JSON")

for ((i = 0; i < TEST_COUNT; i++)); do
  # Extract fields for the current test result by index
  description=$(jq -r ".test_results.details[$i].description" "$TEST_RESULTS_JSON")
  comparison=$(jq -r ".test_results.details[$i].comparison" "$TEST_RESULTS_JSON")
  desired=$(jq -r ".test_results.details[$i].desired" "$TEST_RESULTS_JSON")
  actual=$(jq -r ".test_results.details[$i].actual" "$TEST_RESULTS_JSON")
  units=$(jq -r ".test_results.details[$i].units" "$TEST_RESULTS_JSON")
  result=$(jq -r ".test_results.details[$i].result" "$TEST_RESULTS_JSON")
  notes=$(jq -r ".test_results.details[$i].notes" "$TEST_RESULTS_JSON")

  # Escape comparison field for HTML
  comparison=$(echo "$comparison" | sed 's/<=/\&le;/g; s/>=/\&ge;/g; s/</\&lt;/g; s/>/\&gt;/g; s/==/\&equals;\&equals;/g')

  # Check if desired is an array
  if jq -e ".test_results.details[$i].desired | type == \"array\"" "$TEST_RESULTS_JSON" > /dev/null 2>&1; then
    # Convert array to unordered list
    desired_list=$(jq -r ".test_results.details[$i].desired[]" "$TEST_RESULTS_JSON" | sed 's/$/<br\/>/' | tr '\n' ' ')
    desired="$desired_list"
  else
    # Format desired as a string or number
    desired=$(format_number "$desired")
  fi

  # Add the current test result to the table
  TEST_RESULTS_TABLE+="<tr><td>$description</td><td>$comparison</td><td>$desired</td><td>$(format_number "$actual")</td><td>$units</td><td>$result</td><td>$notes</td></tr>"
done

# Replace test results table section
if [[ -n "$TEST_RESULTS_TABLE" ]]; then
  RESULT=$(process_conditional_section "$RESULT" "TEST_RESULTS_TABLE_SECTION" "true")
  RESULT=$(replace_template_var "$RESULT" "TEST_RESULTS_TABLE" "$TEST_RESULTS_TABLE")
else
  RESULT=$(process_conditional_section "$RESULT" "TEST_RESULTS_TABLE_SECTION" "false")
fi

# Write result to output file
echo "$RESULT" > "$OUTPUT_DIR/index.html"

# Cleanup temporary files
rm -f "$TEMP_HTML"

echo "HTML report generated successfully at: $OUTPUT_DIR/index.html"
