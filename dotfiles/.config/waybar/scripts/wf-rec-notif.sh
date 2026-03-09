#!/usr/bin/env bash

if pgrep wf-recorder > /dev/null; then
  # If wf-recorder is running, output JSON with the recording indicator
  echo '{"text": "🔴"}' 
else
  # If wf-recorder is not running, output empty JSON
  echo '{}'
fi
