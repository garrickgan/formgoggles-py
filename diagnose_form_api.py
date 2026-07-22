#!/usr/bin/env python3
"""Diagnose FORM API workout push steps without touching BLE.

This mirrors the server-side portion of form_sync.py:
login/token -> create workout -> save to user workout list -> fetch protobuf.
It prints status codes and compact response bodies so subscription/entitlement
failures are visible without exposing credentials.
"""

import argparse
import base64
import getpass
import json
import sys

import requests

from form_sync import API_BASE, OAUTH_BASIC, build_api_payload, parse_workout_string


def compact_body(response):
    text = response.text.strip()
    if not text:
        return "<empty>"
    try:
        parsed = response.json()
        return json.dumps(parsed, indent=2, sort_keys=True)[:2000]
    except ValueError:
        return text[:2000]


def login(email, password):
    response = requests.post(
        f"{API_BASE}/oauth/token",
        headers={
            "Authorization": f"Basic {OAUTH_BASIC}",
            "Content-Type": "application/json",
        },
        json={"email": email, "password": password},
        timeout=30,
    )
    print(f"login: HTTP {response.status_code}")
    if response.status_code != 200:
        print(compact_body(response))
        return None
    data = response.json()
    expires = data.get("accessToken", {}).get("expires")
    print(f"login: ok, access token expires {expires}")
    return data["accessToken"]["token"]


def authed_request(method, url, token, **kwargs):
    response = requests.request(
        method,
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=30,
        **kwargs,
    )
    return response


def main():
    parser = argparse.ArgumentParser(description="Diagnose FORM workout API access.")
    parser.add_argument("--token", help="Existing FORM bearer token")
    parser.add_argument("--login", nargs=2, metavar=("EMAIL", "PASSWORD"), help="Log in and use a fresh token")
    parser.add_argument("--login-email", help="Prompt securely for FORM password and log in")
    parser.add_argument("--name", default="Flow State API Diagnostic")
    parser.add_argument(
        "--workout",
        default="warmup: 25 free easy | main: 25 free easy | cooldown: 25 free easy",
        help="Workout string to create for the diagnostic",
    )
    args = parser.parse_args()

    token = args.token
    if args.login:
        token = login(args.login[0], args.login[1])
        if not token:
            return 1
    elif args.login_email:
        password = getpass.getpass("FORM password: ")
        token = login(args.login_email, password)
        if not token:
            return 1

    if not token:
        parser.error("provide --token or --login EMAIL PASSWORD")

    sections = parse_workout_string(args.workout)
    payload = build_api_payload(args.name, sections)

    print("\n1. create workout")
    create_response = authed_request(
        "POST",
        f"{API_BASE}/workout_builder/workouts",
        token,
        json=payload,
    )
    print(f"create: HTTP {create_response.status_code}")
    print(compact_body(create_response))
    if create_response.status_code not in (200, 201):
        return 1

    created = create_response.json()
    workout_id = created["id"]
    print(f"created workout id: {workout_id}")

    print("\n2. fetch protobuf before save")
    early_protobuf_response = authed_request(
        "GET",
        f"{API_BASE}/users/me/workouts/protobuf",
        token,
        params={"workoutIds": workout_id},
    )
    print(f"early protobuf: HTTP {early_protobuf_response.status_code}")
    if early_protobuf_response.status_code == 200:
        early_data = early_protobuf_response.json()
        if early_data:
            binary = base64.b64decode(early_data[0]["binary"])
            print(f"early protobuf: ok, {len(binary)} bytes")
        else:
            print("early protobuf: empty response")
    else:
        print(compact_body(early_protobuf_response))

    print("\n3. save workout to user list")
    save_response = authed_request(
        "POST",
        f"{API_BASE}/users/me/workouts",
        token,
        json={"addWorkoutId": workout_id},
    )
    print(f"save: HTTP {save_response.status_code}")
    print(compact_body(save_response))
    if save_response.status_code != 200:
        print("\nStopped before protobuf fetch because save failed.")
        print("This is the same API step Flow State uses to make the workout visible to FORM sync.")
        return 2

    print("\n4. fetch protobuf after save")
    protobuf_response = authed_request(
        "GET",
        f"{API_BASE}/users/me/workouts/protobuf",
        token,
        params={"workoutIds": workout_id},
    )
    print(f"protobuf: HTTP {protobuf_response.status_code}")
    if protobuf_response.status_code != 200:
        print(compact_body(protobuf_response))
        return 3

    data = protobuf_response.json()
    if not data:
        print("protobuf: empty response")
        return 3

    binary = base64.b64decode(data[0]["binary"])
    print(f"protobuf: ok, {len(binary)} bytes")
    print("\nResult: FORM API create/save/protobuf path works for this account.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
