import copy
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import planner


ROOT = Path(__file__).resolve().parents[1]


def display_minutes(value):
    parsed = datetime.strptime(value, "%I:%M %p")
    return parsed.hour * 60 + parsed.minute


class GroupOutPlannerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.preferences = json.loads((ROOT / "examples/group1.json").read_text())
        cls.places = json.loads((ROOT / "data/places.json").read_text())

    def test_uses_strictest_constraints_and_common_time(self):
        validated = planner.validate_preferences(self.preferences)
        constraints = planner.aggregate_preferences(validated)
        self.assertEqual(constraints["shared_start"], planner.parse_time("19:00"))
        self.assertEqual(constraints["shared_end"], planner.parse_time("22:00"))
        self.assertEqual(constraints["budget_limit"], 40.0)
        self.assertEqual(constraints["distance_limit_miles"], 6.0)
        self.assertEqual(
            constraints["dietary_requirements"], ["gluten-free", "vegetarian"]
        )

    def test_expected_top_plan_is_deterministic(self):
        _, plans = planner.build_ranked_plans(self.preferences, self.places)
        self.assertGreaterEqual(len(plans), 3)
        self.assertEqual(
            plans[0]["name"], "Copper Cactus Kitchen + Cactus Lane Bowling"
        )
        self.assertEqual(plans[0]["score"], 63.2)

    def test_dietary_filter_excludes_incompatible_restaurant(self):
        _, plans = planner.build_ranked_plans(self.preferences, self.places)
        plan_names = [plan["name"] for plan in plans]
        self.assertFalse(any("Tempe Lake Cafe" in name for name in plan_names))

    def test_hard_constraints_hold_for_every_recommendation(self):
        constraints, plans = planner.build_ranked_plans(self.preferences, self.places)
        for plan in plans:
            self.assertLessEqual(
                plan["estimated_cost_per_person"], constraints["budget_limit"]
            )
            self.assertLessEqual(
                plan["maximum_distance_miles"], constraints["distance_limit_miles"]
            )
            self.assertLessEqual(
                display_minutes(plan["stops"][-1]["end"]),
                constraints["shared_end"],
            )

    def test_opening_hours_reject_closed_activities(self):
        places = copy.deepcopy(self.places)
        for place in places:
            if place["type"] == "activity":
                place["close_time"] = "20:00"
        _, plans = planner.build_ranked_plans(self.preferences, places)
        self.assertEqual(plans, [])

    def test_activity_only_mode(self):
        preferences = copy.deepcopy(self.preferences)
        preferences["wants_food"] = False
        _, plans = planner.build_ranked_plans(preferences, self.places)
        self.assertTrue(plans)
        self.assertTrue(all(len(plan["stops"]) == 1 for plan in plans))
        self.assertEqual(plans[0]["stops"][0]["type"], "activity")

    def test_no_overlapping_availability_is_an_input_error(self):
        preferences = copy.deepcopy(self.preferences)
        preferences["members"][0]["available_end"] = "18:30"
        with self.assertRaisesRegex(planner.InputError, "no overlapping availability"):
            planner.build_ranked_plans(preferences, self.places)

    def test_no_feasible_plan_returns_exit_code_three(self):
        preferences = copy.deepcopy(self.preferences)
        for member in preferences["members"]:
            member["max_budget"] = 1
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "group.json"
            places_path = Path(directory) / "places.json"
            output_path = Path(directory) / "output.json"
            input_path.write_text(json.dumps(preferences), encoding="utf-8")
            places_path.write_text(json.dumps(self.places), encoding="utf-8")
            exit_code = planner.main(
                [
                    "--preferences",
                    str(input_path),
                    "--places",
                    str(places_path),
                    "--output",
                    str(output_path),
                ]
            )
            self.assertEqual(exit_code, 3)
            self.assertFalse(output_path.exists())


if __name__ == "__main__":
    unittest.main()
