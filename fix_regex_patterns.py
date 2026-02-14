"""
Fix regex patterns by moving (?i) flags to the start of patterns.

The error "global flags not at the start of the expression" occurs because
patterns like \b(?i)text need to be changed to (?i)\btext.
"""
import os
import re
import glob

def fix_regex_pattern(pattern):
    """
    Fix a regex pattern by moving (?i) flag to the start.

    Examples:
        \b(?i)text -> (?i)\btext
        (?<!x)\b(?i)text -> (?i)(?<!x)\btext
    """
    # Check if pattern has (?i) somewhere other than the start
    if '(?i)' in pattern and not pattern.startswith('(?i)'):
        # Remove all (?i) flags
        pattern_without_flags = pattern.replace('(?i)', '')
        # Add (?i) at the start
        return f'(?i){pattern_without_flags}'
    return pattern


def fix_regex_file(filepath):
    """Fix a single regex file."""
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            original = f.read().strip()

        # Try to compile original
        try:
            re.compile(original)
            return False, "OK"  # Already valid
        except re.error as e:
            if 'global flags not at the start' not in str(e):
                return False, f"Different error: {e}"

        # Fix the pattern
        fixed = fix_regex_pattern(original)

        # Verify the fix works
        try:
            re.compile(fixed)
        except re.error as e:
            return False, f"Fix didn't work: {e}"

        # Write back the fixed pattern
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(fixed)

        return True, "Fixed"

    except Exception as e:
        return False, f"Error: {e}"


def main():
    print("=" * 70)
    print("Fixing Philter Regex Patterns")
    print("=" * 70)

    # Find all .txt files in filters/regex
    regex_files = []
    for root, dirs, files in os.walk('filters/regex'):
        for file in files:
            if file.endswith('.txt'):
                regex_files.append(os.path.join(root, file))

    print(f"\nFound {len(regex_files)} regex pattern files")
    print()

    fixed_count = 0
    error_count = 0
    ok_count = 0

    for filepath in sorted(regex_files):
        success, message = fix_regex_file(filepath)

        if success:
            print(f"FIXED: {filepath}")
            fixed_count += 1
        elif message == "OK":
            ok_count += 1
        else:
            print(f"ERROR: {filepath}")
            print(f"  {message}")
            error_count += 1

    print()
    print("=" * 70)
    print(f"Results:")
    print(f"  Fixed: {fixed_count}")
    print(f"  Already OK: {ok_count}")
    print(f"  Errors: {error_count}")
    print("=" * 70)


if __name__ == "__main__":
    main()
