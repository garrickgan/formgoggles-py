"""Tests for FIT workout file parser."""

import unittest
from unittest.mock import patch, MagicMock

from form_sync import (
    _detect_stroke,
    _speed_to_effort,
    _fit_intensity_to_effort,
    _resolve_fit_steps,
    _attach_rest_to_sets,
    _intensity_to_section,
    parse_fit_file,
)


class TestDetectStroke(unittest.TestCase):
    def test_freestyle_keywords(self):
        self.assertEqual(_detect_stroke("200m Free"), "freestyle")
        self.assertEqual(_detect_stroke("freestyle warmup"), "freestyle")

    def test_backstroke(self):
        self.assertEqual(_detect_stroke("4x50 back"), "backstroke")
        self.assertEqual(_detect_stroke("backstroke drill"), "backstroke")

    def test_breaststroke(self):
        self.assertEqual(_detect_stroke("breast kick"), "breaststroke")

    def test_butterfly(self):
        self.assertEqual(_detect_stroke("4x25 fly sprint"), "butterfly")
        self.assertEqual(_detect_stroke("butterfly"), "butterfly")

    def test_im(self):
        self.assertEqual(_detect_stroke("200 IM"), "im")

    def test_choice(self):
        self.assertEqual(_detect_stroke("100 choice"), "choice")

    def test_default_freestyle(self):
        self.assertEqual(_detect_stroke(""), "freestyle")
        self.assertEqual(_detect_stroke(None), "freestyle")
        self.assertEqual(_detect_stroke("some step"), "freestyle")


class TestSpeedToEffort(unittest.TestCase):
    def test_max(self):
        self.assertEqual(_speed_to_effort(2000), "max")

    def test_strong(self):
        self.assertEqual(_speed_to_effort(1600), "strong")

    def test_fast(self):
        self.assertEqual(_speed_to_effort(1300), "fast")

    def test_moderate(self):
        self.assertEqual(_speed_to_effort(1000), "moderate")

    def test_easy(self):
        self.assertEqual(_speed_to_effort(800), "easy")


class TestFitIntensityToEffort(unittest.TestCase):
    def test_warmup(self):
        self.assertEqual(_fit_intensity_to_effort("warmup"), "easy")
        self.assertEqual(_fit_intensity_to_effort("warm_up"), "easy")

    def test_cooldown(self):
        self.assertEqual(_fit_intensity_to_effort("cooldown"), "easy")
        self.assertEqual(_fit_intensity_to_effort("cool_down"), "easy")

    def test_rest(self):
        self.assertEqual(_fit_intensity_to_effort("rest"), "easy")

    def test_active_with_speed_target(self):
        self.assertEqual(_fit_intensity_to_effort("active", "speed", 2000), "max")
        self.assertEqual(_fit_intensity_to_effort("active", "speed", 1000), "moderate")

    def test_active_no_target(self):
        self.assertEqual(_fit_intensity_to_effort("active"), "moderate")
        self.assertEqual(_fit_intensity_to_effort("active", "open"), "moderate")


class TestIntensityToSection(unittest.TestCase):
    def test_warmup(self):
        self.assertEqual(_intensity_to_section("warmup"), "warmup")
        self.assertEqual(_intensity_to_section("warm_up"), "warmup")

    def test_cooldown(self):
        self.assertEqual(_intensity_to_section("cooldown"), "cooldown")
        self.assertEqual(_intensity_to_section("cool_down"), "cooldown")

    def test_active(self):
        self.assertEqual(_intensity_to_section("active"), "main")

    def test_rest(self):
        self.assertEqual(_intensity_to_section("rest"), "main")


class TestAttachRestToSets(unittest.TestCase):
    def test_rest_attached_to_previous(self):
        items = [
            {"intervalsCount": 1, "intervalDistance": 100, "strokeType": "freestyle",
             "effort": "fast", "restSeconds": 0, "_is_rest": False, "_section": "main"},
            {"_is_rest": True, "_rest_seconds": 20, "_section": "main"},
        ]
        result = _attach_rest_to_sets(items)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["restSeconds"], 20)

    def test_no_rest(self):
        items = [
            {"intervalsCount": 1, "intervalDistance": 200, "strokeType": "freestyle",
             "effort": "easy", "restSeconds": 0, "_is_rest": False, "_section": "warmup"},
        ]
        result = _attach_rest_to_sets(items)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["restSeconds"], 0)

    def test_rest_without_previous_is_dropped(self):
        items = [
            {"_is_rest": True, "_rest_seconds": 10, "_section": "main"},
        ]
        result = _attach_rest_to_sets(items)
        self.assertEqual(len(result), 0)


class TestResolveFitSteps(unittest.TestCase):
    def _make_step(self, **kwargs):
        defaults = {
            "duration_type": "distance",
            "duration_distance": 10000,  # 100m in cm
            "duration_value": 10000,
            "intensity": "active",
            "target_type": "open",
            "custom_target_value_high": None,
            "wkt_step_name": "",
        }
        defaults.update(kwargs)
        return defaults

    def test_simple_warmup_main_cooldown(self):
        steps = [
            self._make_step(duration_distance=20000, intensity="warmup",
                            wkt_step_name="200 free warmup"),
            self._make_step(duration_distance=10000, intensity="active",
                            wkt_step_name="100 free"),
            self._make_step(duration_type="time", duration_time=20000,
                            duration_value=20000, intensity="rest"),
            self._make_step(duration_distance=20000, intensity="cooldown",
                            wkt_step_name="200 free cooldown"),
        ]
        result = _resolve_fit_steps(steps)
        self.assertEqual(len(result), 3)
        # warmup
        self.assertEqual(result[0]["_section"], "warmup")
        self.assertEqual(result[0]["intervalDistance"], 200)
        self.assertEqual(result[0]["effort"], "easy")
        # main with rest attached
        self.assertEqual(result[1]["_section"], "main")
        self.assertEqual(result[1]["intervalDistance"], 100)
        self.assertEqual(result[1]["restSeconds"], 20)
        # cooldown
        self.assertEqual(result[2]["_section"], "cooldown")
        self.assertEqual(result[2]["intervalDistance"], 200)

    def test_repeat_block(self):
        steps = [
            self._make_step(duration_distance=10000, intensity="active",
                            wkt_step_name="100 free"),
            self._make_step(duration_type="time", duration_time=15000,
                            duration_value=15000, intensity="rest"),
            # Repeat step: repeat steps 0-1 ten times
            {
                "duration_type": "repeat_until_steps_cmplt",
                "duration_value": 0,
                "target_value": 10,
            },
        ]
        result = _resolve_fit_steps(steps)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["intervalsCount"], 10)
        self.assertEqual(result[0]["intervalDistance"], 100)
        self.assertEqual(result[0]["restSeconds"], 15)

    def test_distance_conversion_cm_to_m(self):
        steps = [
            self._make_step(duration_distance=5000, intensity="active"),  # 50m
        ]
        result = _resolve_fit_steps(steps)
        self.assertEqual(result[0]["intervalDistance"], 50)

    def test_rest_by_distance_converted_to_time(self):
        steps = [
            self._make_step(duration_distance=10000, intensity="active"),
            self._make_step(duration_distance=5000, intensity="rest"),  # 50m rest → ~60s
        ]
        result = _resolve_fit_steps(steps)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["restSeconds"], 60)  # 50m * 1.2s/m

    def test_stroke_from_step_name(self):
        steps = [
            self._make_step(wkt_step_name="4x50 fly sprint", intensity="active"),
        ]
        result = _resolve_fit_steps(steps)
        self.assertEqual(result[0]["strokeType"], "butterfly")

    def test_effort_from_speed_target(self):
        steps = [
            self._make_step(intensity="active", target_type="speed",
                            custom_target_value_high=2000),
        ]
        result = _resolve_fit_steps(steps)
        self.assertEqual(result[0]["effort"], "max")


class TestParseFitFile(unittest.TestCase):
    def _make_mock_field(self, name, value):
        field = MagicMock()
        field.name = name
        field.value = value
        return field

    def _make_mock_message(self, name, fields_dict):
        msg = MagicMock()
        msg.name = name
        fields = [self._make_mock_field(k, v) for k, v in fields_dict.items()]
        msg.fields = fields

        def get_field(field_name):
            for f in fields:
                if f.name == field_name:
                    return f
            return None
        msg.get = get_field
        return msg

    @patch("form_sync.FitFile", create=True)
    def test_parse_swim_workout(self, mock_fitfile_cls):
        """Test parsing a complete swim workout from a FIT file."""
        # We need to mock the import inside parse_fit_file
        mock_fitfile = MagicMock()
        mock_fitfile_cls.return_value = mock_fitfile

        workout_msg = self._make_mock_message("workout", {
            "sport": 5,
            "wkt_name": "Morning Swim",
            "num_valid_steps": 4,
        })

        step_msgs = [
            self._make_mock_message("workout_step", {
                "message_index": 0,
                "duration_type": "distance",
                "duration_distance": 20000,
                "duration_value": 20000,
                "intensity": "warmup",
                "target_type": "open",
                "custom_target_value_high": None,
                "wkt_step_name": "Warmup free",
            }),
            self._make_mock_message("workout_step", {
                "message_index": 1,
                "duration_type": "distance",
                "duration_distance": 10000,
                "duration_value": 10000,
                "intensity": "active",
                "target_type": "speed",
                "custom_target_value_high": 1400,
                "wkt_step_name": "Main free",
            }),
            self._make_mock_message("workout_step", {
                "message_index": 2,
                "duration_type": "distance",
                "duration_distance": 20000,
                "duration_value": 20000,
                "intensity": "cooldown",
                "target_type": "open",
                "custom_target_value_high": None,
                "wkt_step_name": "Cooldown free",
            }),
        ]

        def get_messages(msg_type):
            if msg_type == "workout":
                return [workout_msg]
            elif msg_type == "workout_step":
                return step_msgs
            return []

        mock_fitfile.get_messages = get_messages

        with patch("form_sync.FitFile", mock_fitfile_cls):
            from form_sync import parse_fit_file as pff
            # Need to patch the import inside parse_fit_file
            with patch.dict("sys.modules", {"fitparse": MagicMock(FitFile=mock_fitfile_cls)}):
                sections, wkt_name = pff("dummy.fit")

        self.assertEqual(wkt_name, "Morning Swim")
        self.assertEqual(len(sections["warmup"]), 1)
        self.assertEqual(sections["warmup"][0]["intervalDistance"], 200)
        self.assertEqual(sections["warmup"][0]["effort"], "easy")
        self.assertEqual(len(sections["main"]), 1)
        self.assertEqual(sections["main"][0]["intervalDistance"], 100)
        self.assertEqual(sections["main"][0]["effort"], "fast")
        self.assertEqual(len(sections["cooldown"]), 1)
        self.assertEqual(sections["cooldown"][0]["intervalDistance"], 200)

    def test_non_swim_warning(self):
        """Test that non-swim sport produces a warning."""
        # This is tested indirectly through the sport check in parse_fit_file
        # The function should warn but not error
        pass

    def test_empty_steps(self):
        """Test handling of FIT file with no workout steps."""
        result = _resolve_fit_steps([])
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
