#!/usr/bin/env bash

# Loop forever and toggle the active virtual machine every minute.
# This would emulate the behavior of many security camera viewing solutions.
while :; do
  dbus-send \
    --system \
    --type="method_call" \
    --dest=dev.akeydo \
    /dev/akeydo \
    dev.akeydo.Toggle
  sleep 60
done
