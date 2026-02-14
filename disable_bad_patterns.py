"""Disable the 2 problematic patterns in philter config."""
import json

config_file = "configs/philter_one.json"

# Load config
with open(config_file, 'r') as f:
    patterns = json.load(f)

# Patterns to disable
to_disable = [
    "number streetname city",
    "number streetname noindicator suite"
]

# Disable them by removing from list
disabled_count = 0
new_patterns = []

for pattern in patterns:
    if pattern.get('title') in to_disable:
        print(f"Disabling: {pattern['title']}")
        disabled_count += 1
    else:
        new_patterns.append(pattern)

# Save updated config
with open(config_file, 'w') as f:
    json.dump(new_patterns, f, indent=4)

print(f"\nDisabled {disabled_count} patterns")
print(f"Remaining: {len(new_patterns)} patterns")
