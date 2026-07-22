#!/usr/bin/env python3
"""Local-only web UI for diagnosing FORM API access.

Runs on 127.0.0.1 and does not log or store the FORM password.
"""

import base64
import html
import json

import requests
from flask import Flask, request

from form_sync import API_BASE, OAUTH_BASIC, build_api_payload, parse_workout_string


app = Flask(__name__)


def compact_body(response):
    text = response.text.strip()
    if not text:
        return "<empty>"
    try:
        return json.dumps(response.json(), indent=2, sort_keys=True)[:2000]
    except ValueError:
        return text[:2000]


def authed_request(method, url, token, **kwargs):
    return requests.request(
        method,
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=30,
        **kwargs,
    )


def run_diagnostic(email, password):
    lines = []

    login_response = requests.post(
        f"{API_BASE}/oauth/token",
        headers={
            "Authorization": f"Basic {OAUTH_BASIC}",
            "Content-Type": "application/json",
        },
        json={"email": email, "password": password},
        timeout=30,
    )
    lines.append(f"login: HTTP {login_response.status_code}")
    if login_response.status_code != 200:
        lines.append(compact_body(login_response))
        return "\n".join(lines)

    data = login_response.json()
    token = data["accessToken"]["token"]
    lines.append(f"login: ok, access token expires {data.get('accessToken', {}).get('expires')}")

    sections = parse_workout_string("warmup: 25 free easy | main: 25 free easy | cooldown: 25 free easy")
    payload = build_api_payload("Flow State API Diagnostic", sections)

    lines.append("\n1. create workout")
    create_response = authed_request(
        "POST",
        f"{API_BASE}/workout_builder/workouts",
        token,
        json=payload,
    )
    lines.append(f"create: HTTP {create_response.status_code}")
    lines.append(compact_body(create_response))
    if create_response.status_code not in (200, 201):
        return "\n".join(lines)

    workout_id = create_response.json()["id"]
    lines.append(f"created workout id: {workout_id}")

    lines.append("\n2. fetch protobuf before save")
    early_response = authed_request(
        "GET",
        f"{API_BASE}/users/me/workouts/protobuf",
        token,
        params={"workoutIds": workout_id},
    )
    lines.append(f"early protobuf: HTTP {early_response.status_code}")
    if early_response.status_code == 200:
        early_data = early_response.json()
        if early_data:
            binary = base64.b64decode(early_data[0]["binary"])
            lines.append(f"early protobuf: ok, {len(binary)} bytes")
        else:
            lines.append("early protobuf: empty response")
    else:
        lines.append(compact_body(early_response))

    lines.append("\n3. save workout to user list")
    save_response = authed_request(
        "POST",
        f"{API_BASE}/users/me/workouts",
        token,
        json={"addWorkoutId": workout_id},
    )
    lines.append(f"save: HTTP {save_response.status_code}")
    lines.append(compact_body(save_response))
    if save_response.status_code != 200:
        lines.append("\nStopped before after-save protobuf fetch because save failed.")
        return "\n".join(lines)

    lines.append("\n4. fetch protobuf after save")
    protobuf_response = authed_request(
        "GET",
        f"{API_BASE}/users/me/workouts/protobuf",
        token,
        params={"workoutIds": workout_id},
    )
    lines.append(f"protobuf: HTTP {protobuf_response.status_code}")
    if protobuf_response.status_code != 200:
        lines.append(compact_body(protobuf_response))
        return "\n".join(lines)

    proto_data = protobuf_response.json()
    if not proto_data:
        lines.append("protobuf: empty response")
        return "\n".join(lines)

    binary = base64.b64decode(proto_data[0]["binary"])
    lines.append(f"protobuf: ok, {len(binary)} bytes")
    return "\n".join(lines)


@app.get("/")
def index():
    return """<!doctype html>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FORM API Diagnostic</title>
<style>
body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 40px; max-width: 760px; }
label { display: block; margin: 16px 0 6px; font-weight: 600; }
input { font: inherit; padding: 10px; width: 100%; box-sizing: border-box; }
button { margin-top: 18px; padding: 10px 14px; font: inherit; }
p { color: #555; }
</style>
<h1>FORM API Diagnostic</h1>
<p>This runs locally on your laptop. The password is sent only to FORM's login endpoint and is not saved.</p>
<form method="post">
  <label>FORM email</label>
  <input name="email" value="ggan93@gmail.com" autocomplete="username">
  <label>FORM password</label>
  <input name="password" type="password" autocomplete="current-password" autofocus>
  <button type="submit">Run diagnostic</button>
</form>"""


@app.post("/")
def submit():
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    if not email or not password:
        result = "Missing email or password."
    else:
        try:
            result = run_diagnostic(email, password)
        except Exception as exc:
            result = f"diagnostic crashed: {exc}"

    escaped = html.escape(result)
    return f"""<!doctype html>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FORM API Diagnostic Result</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 40px; max-width: 980px; }}
pre {{ white-space: pre-wrap; background: #111; color: #eee; padding: 18px; border-radius: 8px; }}
a {{ display: inline-block; margin-top: 16px; }}
</style>
<h1>Result</h1>
<pre>{escaped}</pre>
<a href="/">Run again</a>"""


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5051, debug=False)
