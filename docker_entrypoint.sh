#!/bin/sh

set -e

DEVICE_ID=${DEVICE_ID:?}
DEVICE_PASSWORD=${DEVICE_PASSWORD:?}

python -u /app/v380_stream.py -d ${DEVICE_ID} -p ${DEVICE_PASSWORD} ${ADDITIONAL_OPTIONS}
