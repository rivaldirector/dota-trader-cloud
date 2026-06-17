#!/usr/bin/env python3
"""Parse the DotaScore OpenAPI spec from the cached web_fetch result."""
import json, sys, re, glob, os

# Try both possible cached file paths
candidates = [
    "/var/folders/s3/2l4lnbnd7qgcvymnlwzps0lm0000gn/T/claude-hostloop-plugins/e38a8e2c707f10d7/projects/-Users-yanpushkaryov-Library-Application-Support-Claude-local-agent-mode-sessions-063fe51c-c2c7-4e4e-a189-91a89eea5dd2-c0b95781-5eef-4aa9-a5c5-b42778f7aab1-local-1f997749-1efb-452e-a07d-0b6a628f65ae-o-36ro9/342c3fd3-37af-43ab-926c-bbe00e1d1362/tool-results/mcp-workspace-web_fetch-1781479120418.txt",
    "/var/folders/s3/2l4lnbnd7qgcvymnlwzps0lm0000gn/T/claude-hostloop-plugins/e38a8e2c707f10d7/projects/-Users-yanpushkaryov-Library-Application-Support-Claude-local-agent-mode-sessions-063fe51c-c2c7-4e4e-a189-91a89eea5dd2-c0b95781-5eef-4aa9-a5c5-b42778f7aab1-local-1f997749-1efb-452e-a07d-0b6a628f65ae-o-36ro9/342c3fd3-37af-43ab-926c-bbe00e1d1362/tool-results/mcp-workspace-web_fetch-1781479047163.txt",
]

raw = None
for path in candidates:
    if os.path.exists(path):
        with open(path, 'r') as f:
            raw = f.read()
        print(f"Read file: {path}, length={len(raw)}", file=sys.stderr)
        break

if raw is None:
    print("ERROR: no cached file found", file=sys.stderr)
    sys.exit(1)

# The web_fetch result format is:
# Line 1: original URL
# Line 2: → redirected URL
# Line 3: Content-Type: ...
# Line 4: (blank)
# Line 5: the JSON body
lines = raw.split('\n')
print(f"Total lines: {len(lines)}", file=sys.stderr)
for i, line in enumerate(lines):
    print(f"Line {i+1} length: {len(line)}", file=sys.stderr)

# Find the JSON line (longest one, or the one starting with {)
json_str = None
for line in lines:
    stripped = line.strip()
    if stripped.startswith('{'):
        json_str = stripped
        break

if json_str is None:
    print("ERROR: could not find JSON line", file=sys.stderr)
    sys.exit(1)

print(f"JSON string length: {len(json_str)}", file=sys.stderr)

data = json.loads(json_str)

OUTPUT = []

# ============================================================
# 1. securitySchemes
# ============================================================
OUTPUT.append("=" * 60)
OUTPUT.append("SECTION 1: securitySchemes")
OUTPUT.append("=" * 60)
components = data.get("components", {})
schemes = components.get("securitySchemes", {})
if schemes:
    OUTPUT.append(json.dumps(schemes, indent=2))
else:
    OUTPUT.append("No securitySchemes in components.securitySchemes")
    # Try swagger 2.0
    sec_defs = data.get("securityDefinitions", {})
    if sec_defs:
        OUTPUT.append("securityDefinitions:")
        OUTPUT.append(json.dumps(sec_defs, indent=2))
    else:
        OUTPUT.append("No securityDefinitions either")
    top_sec = data.get("security", [])
    OUTPUT.append(f"top-level security: {json.dumps(top_sec)}")

# Also check first endpoint's security/parameters for auth clues
paths = data.get("paths", {})
first_path = next(iter(paths.values()), {})
first_method = next(iter(first_path.values()), {})
params = first_method.get("parameters", [])
auth_params = [p for p in params if "authorization" in p.get("name","").lower() or "api" in p.get("name","").lower()]
if auth_params:
    OUTPUT.append("\nAuth-related params on first endpoint:")
    OUTPUT.append(json.dumps(auth_params, indent=2))

# ============================================================
# 2. All paths + HTTP method + summary
# ============================================================
OUTPUT.append("\n" + "=" * 60)
OUTPUT.append("SECTION 2: All paths + HTTP method + summary")
OUTPUT.append("=" * 60)
for path, methods in sorted(paths.items()):
    for method, spec in methods.items():
        if method.upper() in ("GET","POST","PUT","DELETE","PATCH","HEAD","OPTIONS"):
            summary = spec.get("summary","(no summary)")
            OUTPUT.append(f"{method.upper():7s}  {path}  —  {summary}")

# ============================================================
# 3. Full detail on paths containing "odd" in the URL
# ============================================================
OUTPUT.append("\n" + "=" * 60)
OUTPUT.append("SECTION 3: Full detail — paths containing 'odd'")
OUTPUT.append("=" * 60)
for path, methods in paths.items():
    if "odd" in path.lower():
        OUTPUT.append(f"\n--- PATH: {path} ---")
        OUTPUT.append(json.dumps(methods, indent=2))

# ============================================================
# 4. Full detail on /dota2/matches (not sub-paths)
# ============================================================
OUTPUT.append("\n" + "=" * 60)
OUTPUT.append("SECTION 4: Full detail — /dota2/matches endpoint parameters")
OUTPUT.append("=" * 60)
for path, methods in paths.items():
    if path in ("/dota2/matches", "/matches", "/dota2/matches/"):
        OUTPUT.append(f"\n--- PATH: {path} ---")
        OUTPUT.append(json.dumps(methods, indent=2))

print("\n".join(OUTPUT))
