#!/usr/bin/env python3
"""Deterministic rule-based baseline for GroupOut Planner."""

from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any


TRAVEL_BUFFER_MINUTES = 20


class InputError(ValueError):
    """Raised when an input file does not match the expected schema."""


def parse_time(value: str) -> int:
    """Convert a 24-hour HH:MM string to minutes after midnight."""
    if not isinstance(value, str):
        raise InputError(f"Expected a time string, received {type(value).__name__}.")
    try:
        hour_text, minute_text = value.split(":", maxsplit=1)
        hour, minute = int(hour_text), int(minute_text)
    except (ValueError, AttributeError) as exc:
        raise InputError(f"Invalid time '{value}'. Use 24-hour HH:MM format.") from exc
    if hour == 24 and minute == 0:
        return 1440
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise InputError(f"Invalid time '{value}'. Use 24-hour HH:MM format.")
    return hour * 60 + minute


def format_time(minutes: int) -> str:
    """Format minutes after midnight as a human-readable time."""
    if minutes == 1440:
        return "12:00 AM"
    hour = (minutes // 60) % 24
    minute = minutes % 60
    suffix = "AM" if hour < 12 else "PM"
    display_hour = hour % 12 or 12
    return f"{display_hour}:{minute:02d} {suffix}"


def load_json(path: str | Path) -> Any:
    """Load JSON and report actionable file/format errors."""
    source = Path(path)
    try:
        with source.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise InputError(f"File not found: {source}") from exc
    except json.JSONDecodeError as exc:
        raise InputError(
            f"Invalid JSON in {source} at line {exc.lineno}, column {exc.colno}."
        ) from exc


def _require(
    record: dict[str, Any], key: str, expected: type | tuple[type, ...], context: str
) -> Any:
    if key not in record:
        raise InputError(f"Missing '{key}' in {context}.")
    value = record[key]
    if not isinstance(value, expected):
        if isinstance(expected, tuple):
            expected_name = " or ".join(item.__name__ for item in expected)
        else:
            expected_name = expected.__name__
        raise InputError(f"'{key}' in {context} must be {expected_name}.")
    return value


def validate_preferences(preferences: Any) -> dict[str, Any]:
    """Validate and normalize a group-preference object."""
    if not isinstance(preferences, dict):
        raise InputError("The preferences file must contain one JSON object.")
    normalized = deepcopy(preferences)
    _require(normalized, "group_name", str, "preferences")
    _require(normalized, "meeting_location", str, "preferences")
    _require(normalized, "wants_food", bool, "preferences")
    members = _require(normalized, "members", list, "preferences")
    if not members:
        raise InputError("The preferences file must contain at least one member.")

    for index, member in enumerate(members, start=1):
        context = f"member {index}"
        if not isinstance(member, dict):
            raise InputError(f"{context.capitalize()} must be a JSON object.")
        _require(member, "name", str, context)
        start = parse_time(_require(member, "available_start", str, context))
        end = parse_time(_require(member, "available_end", str, context))
        if end <= start:
            raise InputError(f"{context.capitalize()} must end after it starts.")
        budget = _require(member, "max_budget", (int, float), context)
        distance = _require(member, "max_distance_miles", (int, float), context)
        restrictions = _require(member, "dietary_restrictions", list, context)
        preferences_list = _require(member, "preferred_activities", list, context)
        if budget <= 0 or distance <= 0:
            raise InputError(f"Budget and distance in {context} must be greater than zero.")
        if not all(isinstance(item, str) for item in restrictions + preferences_list):
            raise InputError(f"Restrictions and activity preferences in {context} must be strings.")
        member["dietary_restrictions"] = sorted({item.strip().lower() for item in restrictions})
        member["preferred_activities"] = sorted({item.strip().lower() for item in preferences_list})
    return normalized


def validate_places(places: Any) -> list[dict[str, Any]]:
    """Validate and normalize static place records."""
    if not isinstance(places, list) or not places:
        raise InputError("The places file must contain a non-empty JSON list.")
    normalized = deepcopy(places)
    seen_names: set[str] = set()
    for index, place in enumerate(normalized, start=1):
        context = f"place {index}"
        if not isinstance(place, dict):
            raise InputError(f"{context.capitalize()} must be a JSON object.")
        name = _require(place, "name", str, context)
        if name in seen_names:
            raise InputError(f"Duplicate place name: {name}")
        seen_names.add(name)
        place_type = _require(place, "type", str, context).lower()
        if place_type not in {"restaurant", "activity"}:
            raise InputError(f"'{name}' must have type 'restaurant' or 'activity'.")
        place["type"] = place_type
        place["category"] = _require(place, "category", str, context).strip().lower()
        cost = _require(place, "cost_per_person", (int, float), context)
        distance = _require(place, "distance_miles", (int, float), context)
        duration = _require(place, "duration_minutes", int, context)
        if cost < 0 or distance < 0 or duration <= 0:
            raise InputError(f"Cost and distance for '{name}' cannot be negative; duration must be positive.")
        open_minutes = parse_time(_require(place, "open_time", str, context))
        close_minutes = parse_time(_require(place, "close_time", str, context))
        if close_minutes <= open_minutes:
            raise InputError(f"'{name}' must close after it opens; overnight hours are out of scope.")
        dietary = _require(place, "dietary_options", list, context)
        if not all(isinstance(item, str) for item in dietary):
            raise InputError(f"Dietary options for '{name}' must be strings.")
        place["dietary_options"] = sorted({item.strip().lower() for item in dietary})
        place["_open_minutes"] = open_minutes
        place["_close_minutes"] = close_minutes
    return normalized


def aggregate_preferences(preferences: dict[str, Any]) -> dict[str, Any]:
    """Combine member inputs using conservative group constraints."""
    members = preferences["members"]
    shared_start = max(parse_time(member["available_start"]) for member in members)
    shared_end = min(parse_time(member["available_end"]) for member in members)
    if shared_end <= shared_start:
        raise InputError("The group has no overlapping availability.")
    return {
        "shared_start": shared_start,
        "shared_end": shared_end,
        "budget_limit": float(min(member["max_budget"] for member in members)),
        "distance_limit_miles": float(min(member["max_distance_miles"] for member in members)),
        "dietary_requirements": sorted(
            {restriction for member in members for restriction in member["dietary_restrictions"]}
        ),
    }


def _is_open(place: dict[str, Any], start: int, end: int) -> bool:
    return place["_open_minutes"] <= start and end <= place["_close_minutes"]


def _score_plan(
    activity: dict[str, Any],
    members: list[dict[str, Any]],
    total_cost: float,
    maximum_distance: float,
    constraints: dict[str, Any],
    plan_end: int,
) -> tuple[float, dict[str, float], int]:
    matching_members = sum(
        activity["category"] in member["preferred_activities"] for member in members
    )
    preference_points = 50 * matching_members / len(members)
    budget_points = 25 * max(0.0, 1 - total_cost / constraints["budget_limit"])
    distance_points = 15 * max(
        0.0, 1 - maximum_distance / constraints["distance_limit_miles"]
    )
    remaining_minutes = constraints["shared_end"] - plan_end
    time_points = 10 * min(max(remaining_minutes, 0) / 60, 1)
    components = {
        "activity_preference": round(preference_points, 1),
        "budget_headroom": round(budget_points, 1),
        "distance": round(distance_points, 1),
        "time_window_fit": round(time_points, 1),
    }
    return round(sum(components.values()), 1), components, matching_members


def build_ranked_plans(
    preferences: dict[str, Any], places: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Generate, filter, score, and rank all feasible plans."""
    preferences = validate_preferences(preferences)
    places = validate_places(places)
    constraints = aggregate_preferences(preferences)
    members = preferences["members"]
    restaurants = sorted(
        (place for place in places if place["type"] == "restaurant"),
        key=lambda item: item["name"],
    )
    activities = sorted(
        (place for place in places if place["type"] == "activity"),
        key=lambda item: item["name"],
    )
    plans: list[dict[str, Any]] = []

    restaurant_options: list[dict[str, Any] | None]
    restaurant_options = restaurants if preferences["wants_food"] else [None]
    required_diets = set(constraints["dietary_requirements"])

    for restaurant in restaurant_options:
        for activity in activities:
            start = constraints["shared_start"]
            stops: list[dict[str, Any]] = []
            total_cost = float(activity["cost_per_person"])
            maximum_distance = float(activity["distance_miles"])

            if restaurant is not None:
                if not required_diets.issubset(set(restaurant["dietary_options"])):
                    continue
                restaurant_end = start + restaurant["duration_minutes"]
                if not _is_open(restaurant, start, restaurant_end):
                    continue
                stops.append(
                    {
                        "name": restaurant["name"],
                        "type": "restaurant",
                        "category": restaurant["category"],
                        "start": format_time(start),
                        "end": format_time(restaurant_end),
                    }
                )
                activity_start = restaurant_end + TRAVEL_BUFFER_MINUTES
                total_cost += float(restaurant["cost_per_person"])
                maximum_distance = max(maximum_distance, float(restaurant["distance_miles"]))
            else:
                activity_start = start

            activity_end = activity_start + activity["duration_minutes"]
            if total_cost > constraints["budget_limit"]:
                continue
            if maximum_distance > constraints["distance_limit_miles"]:
                continue
            if activity_end > constraints["shared_end"] or not _is_open(
                activity, activity_start, activity_end
            ):
                continue

            stops.append(
                {
                    "name": activity["name"],
                    "type": "activity",
                    "category": activity["category"],
                    "start": format_time(activity_start),
                    "end": format_time(activity_end),
                }
            )
            score, score_components, matching_members = _score_plan(
                activity,
                members,
                total_cost,
                maximum_distance,
                constraints,
                activity_end,
            )
            name = " + ".join(stop["name"] for stop in stops)
            reasons = [
                f"{activity['category'].title()} matches {matching_members} of {len(members)} members' activity preferences.",
                f"The estimated ${total_cost:.2f} cost is within the ${constraints['budget_limit']:.2f} per-person limit.",
                f"Every stop is within the {constraints['distance_limit_miles']:.1f}-mile group limit.",
                f"The schedule fits the shared {format_time(constraints['shared_start'])}-{format_time(constraints['shared_end'])} window.",
            ]
            if restaurant is not None and required_diets:
                reasons.insert(
                    1,
                    "The restaurant supports " + ", ".join(sorted(required_diets)) + ".",
                )
            plans.append(
                {
                    "name": name,
                    "schedule": f"{format_time(start)}-{format_time(activity_end)}",
                    "estimated_cost_per_person": round(total_cost, 2),
                    "maximum_distance_miles": round(maximum_distance, 1),
                    "score": score,
                    "score_components": score_components,
                    "stops": stops,
                    "reasons": reasons,
                }
            )

    plans.sort(key=lambda plan: (-plan["score"], plan["name"]))
    return constraints, plans


def build_result(
    preferences: dict[str, Any], constraints: dict[str, Any], plans: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "group_name": preferences["group_name"],
        "meeting_location": preferences["meeting_location"],
        "method": "Static rule-based filtering and deterministic ranking",
        "constraints": {
            "shared_availability": (
                f"{format_time(constraints['shared_start'])}-{format_time(constraints['shared_end'])}"
            ),
            "budget_limit_per_person": constraints["budget_limit"],
            "distance_limit_miles": constraints["distance_limit_miles"],
            "dietary_requirements": constraints["dietary_requirements"],
        },
        "recommendations": plans[:3],
    }


def print_result(result: dict[str, Any]) -> None:
    constraints = result["constraints"]
    diets = ", ".join(constraints["dietary_requirements"]) or "none"
    print("GroupOut Planner - Baseline Results")
    print(f"Group: {result['group_name']}")
    print(f"Meeting location: {result['meeting_location']}")
    print(f"Common availability: {constraints['shared_availability']}")
    print(f"Budget limit: ${constraints['budget_limit_per_person']:.2f} per person")
    print(f"Distance limit: {constraints['distance_limit_miles']:.1f} miles")
    print(f"Dietary requirements: {diets}")
    print()
    for index, plan in enumerate(result["recommendations"], start=1):
        print(f"{index}. {plan['name']}")
        print(f"   Schedule: {plan['schedule']}")
        print(f"   Estimated cost: ${plan['estimated_cost_per_person']:.2f} per person")
        print(f"   Maximum distance: {plan['maximum_distance_miles']:.1f} miles")
        print(f"   Score: {plan['score']:.1f}/100")
        print("   Why selected:")
        for reason in plan["reasons"]:
            print(f"   - {reason}")
        print()


def write_result(result: dict[str, Any], output_path: str | Path) -> None:
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, ensure_ascii=True)
        handle.write("\n")


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Recommend deterministic group-night plans from static JSON data."
    )
    parser.add_argument("--preferences", required=True, help="Path to group preferences JSON")
    parser.add_argument("--places", required=True, help="Path to static places JSON")
    parser.add_argument("--output", required=True, help="Path for result JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    try:
        raw_preferences = load_json(args.preferences)
        raw_places = load_json(args.places)
        validated_preferences = validate_preferences(raw_preferences)
        constraints, plans = build_ranked_plans(validated_preferences, raw_places)
        if not plans:
            print("No feasible plans satisfy every group constraint.", file=sys.stderr)
            return 3
        result = build_result(validated_preferences, constraints, plans)
        print_result(result)
        write_result(result, args.output)
        print(f"Saved JSON results to: {args.output}")
        return 0
    except InputError as exc:
        print(f"Input error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
