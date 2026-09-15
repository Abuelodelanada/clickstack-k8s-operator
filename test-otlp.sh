#!/bin/bash
NOW_NANO="$(date +%s)000000000"

curl -i "http://10.1.0.207:4318/v1/logs" \
  -H "Content-Type: application/json" \
  -H "authorization: ${CLICKSTACK_API_KEY}" \
  --data-binary @- <<EOF
{
  "resourceLogs": [{
    "resource": {
      "attributes": [{
        "key": "service.name",
        "value": {"stringValue": "clickstack-docs-test"}
      }]
    },
    "scopeLogs": [{
      "scope": {"name": "clickstack-docs-test"},
      "logRecords": [{
        "timeUnixNano": "${NOW_NANO}",
        "severityText": "INFO",
        "body": {"stringValue": "ClickStack ingestion test"}
      }]
    }]
  }]
}
EOF
