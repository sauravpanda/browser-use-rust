import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

import browser_use_rs._extra_tools as extra_tools  # noqa: E402
from browser_use_rs._extra_tools import (  # noqa: E402
    EXTRA_STATELESS_TOOLS,
    va_facility_locator,
)
from browser_use_rs.agent import Agent  # noqa: E402


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


def _facility(name, address1, city, state, zip_code, lat, lon, address2=""):
    physical = {
        "address1": address1,
        "city": city,
        "state": state,
        "zip": zip_code,
    }
    if address2:
        physical["address2"] = address2
    return {
        "id": name.lower().replace(" ", "_"),
        "attributes": {
            "name": name,
            "lat": lat,
            "long": lon,
            "address": {"physical": physical},
        },
    }


class VAFacilityLocatorTests(unittest.TestCase):
    def _fake_urlopen(self, calls, payload):
        def fake(req, timeout=0):
            calls.append((req, timeout))
            return _Response(payload)

        return fake

    def test_va_facility_locator_calls_official_api_and_sorts_by_distance(self):
        calls = []
        payload = {
            "data": [
                _facility(
                    "Franklin Street VA Clinic",
                    "1500 Franklin Street Northeast",
                    "Washington",
                    "DC",
                    "20018-2000",
                    38.926008,
                    -76.983624,
                    "Community Resource & Referral Center (CRRC)",
                ),
                _facility(
                    "Washington VA Medical Center",
                    "50 Irving Street, Northwest",
                    "Washington",
                    "DC",
                    "20422-0001",
                    38.929401,
                    -77.0111955,
                ),
                _facility(
                    "Southeast Washington VA Clinic",
                    "820 Chesapeake Street, Southeast",
                    "Washington",
                    "DC",
                    "20032-3428",
                    38.829393,
                    -76.9924845,
                ),
            ]
        }

        with patch.object(extra_tools, "urlopen", self._fake_urlopen(calls, payload)):
            out = asyncio.run(
                va_facility_locator.func(object(), "Arlington, VA", limit=3)
            )

        self.assertEqual(len(calls), 1)
        req, timeout = calls[0]
        self.assertEqual(req.full_url, "https://api.va.gov/facilities_api/v2/va")
        self.assertEqual(timeout, 15)
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["type"], "health")
        self.assertIs(body["mobile"], False)
        self.assertEqual(body["per_page"], 10)
        self.assertIn("bbox", body)

        self.assertIn("VA.gov facility locator results for Arlington, VA", out)
        self.assertLess(
            out.index("Washington VA Medical Center"),
            out.index("Southeast Washington VA Clinic"),
        )
        self.assertLess(
            out.index("Southeast Washington VA Clinic"),
            out.index("Franklin Street VA Clinic"),
        )
        self.assertIn("Community Resource & Referral Center (CRRC)", out)

    def test_va_facility_locator_maps_service_type(self):
        calls = []
        payload = {
            "data": [
                _facility(
                    "Washington VA Medical Center",
                    "50 Irving Street, Northwest",
                    "Washington",
                    "DC",
                    "20422-0001",
                    38.929401,
                    -77.0111955,
                )
            ]
        }

        with patch.object(extra_tools, "urlopen", self._fake_urlopen(calls, payload)):
            asyncio.run(
                va_facility_locator.func(
                    object(),
                    "Arlington, VA",
                    limit=1,
                    facility_type="VA health",
                    service_type="Primary care",
                )
            )

        body = json.loads(calls[0][0].data.decode("utf-8"))
        self.assertEqual(body["type"], "health")
        self.assertEqual(body["services"], ["PrimaryCare"])

    def test_va_facility_locator_is_registered_as_read_only_tool(self):
        names = [tool.name for tool in EXTRA_STATELESS_TOOLS]

        self.assertIn("va_facility_locator", names)
        self.assertIn("va_facility_locator", Agent._READ_ONLY_CANONICAL)


if __name__ == "__main__":
    unittest.main()
