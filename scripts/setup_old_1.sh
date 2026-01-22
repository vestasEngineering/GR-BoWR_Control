#!/bin/bash
echo "Running setup script"
echo "Adjust user to allow serial comms"
echo $(sudo usermod -a -G dialout $USER)
