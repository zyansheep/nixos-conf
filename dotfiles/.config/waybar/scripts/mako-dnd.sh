#!/bin/sh
# Waybar custom module for mako do-not-disturb toggle
# Outputs JSON for waybar; toggled via on-click

if makoctl mode | grep -q "do-not-disturb"; then
    echo '{"text": "󰂛", "tooltip": "Do Not Disturb: ON", "class": "dnd-on"}'
else
    echo '{"text": "󰂚", "tooltip": "Do Not Disturb: OFF", "class": "dnd-off"}'
fi
