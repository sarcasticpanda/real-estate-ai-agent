import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.main import _safe_image_filename, app
from broker.upload_handler import enrich_property


class BrokerPropertyManagementTests(unittest.TestCase):
    def setUp(self):
        self.previous_token = os.environ.get("BROKER_TOKEN")
        self.previous_app_secret = os.environ.get("WHATSAPP_APP_SECRET")
        os.environ["BROKER_TOKEN"] = "test-secret"
        self.client = TestClient(app)

    def tearDown(self):
        if self.previous_token is None:
            os.environ.pop("BROKER_TOKEN", None)
        else:
            os.environ["BROKER_TOKEN"] = self.previous_token
        if self.previous_app_secret is None:
            os.environ.pop("WHATSAPP_APP_SECRET", None)
        else:
            os.environ["WHATSAPP_APP_SECRET"] = self.previous_app_secret

    def test_csv_import_requires_broker_token(self):
        response = self.client.post(
            "/upload",
            data={"token": "wrong"},
            files={"file": ("properties.csv", b"property_type,price_inr,address\nFlat,5000000,Gomti Nagar", "text/csv")},
        )
        self.assertEqual(response.status_code, 401)

    def test_csv_import_rejects_unknown_mapping(self):
        response = self.client.post(
            "/upload",
            data={"token": "test-secret", "column_map": '{"price_inr":"Missing"}'},
            files={"file": ("properties.csv", b"Type,Price,Address\nFlat,5000000,Gomti Nagar", "text/csv")},
        )
        self.assertEqual(response.status_code, 400)

    def test_browse_rejects_inverted_price_range(self):
        response = self.client.get("/api/properties/browse?min_price=900&max_price=100")
        self.assertEqual(response.status_code, 400)

    def test_pipeline_rejects_unknown_stage(self):
        response = self.client.post(
            "/broker/leads/lead-1/status",
            json={"token": "test-secret", "status": "converted"},
        )
        self.assertEqual(response.status_code, 400)

    def test_whatsapp_rejects_invalid_signature_when_secret_is_configured(self):
        os.environ["WHATSAPP_APP_SECRET"] = "meta-secret"
        response = self.client.post(
            "/webhook/whatsapp",
            content=b'{"entry":[]}',
            headers={"X-Hub-Signature-256": "sha256=wrong"},
        )
        self.assertEqual(response.status_code, 403)

    @patch("database.supabase_client.get_client")
    def test_analytics_uses_won_and_confirmed_definitions(self, get_client):
        get_client.return_value = _FakeAnalyticsClient({
            "leads": [
                {"status": "won", "created_at": "2026-06-18T10:00:00", "preferred_area": "Gomti Nagar", "preferred_bhk": 3, "budget_max": 1},
                {"status": "visit", "created_at": "2026-06-17T10:00:00", "preferred_area": "Aliganj", "preferred_bhk": 2, "budget_max": 1},
            ],
            "meetings": [
                {"status": "confirmed", "scheduled_at": "2026-06-20T10:00:00", "created_at": "2026-06-18T10:00:00"},
                {"status": "cancelled", "scheduled_at": "2026-06-20T11:00:00", "created_at": "2026-06-18T10:00:00"},
            ],
            "properties": [{"status": "available", "property_type": "Flat", "area_name": "Gomti Nagar"}],
        })

        response = self.client.get("/api/broker/analytics/charts?token=test-secret")

        self.assertEqual(response.status_code, 200)
        summary = response.json()["summary"]
        self.assertEqual(summary["won"], 1)
        self.assertEqual(summary["conversion_pct"], 50.0)
        self.assertEqual(summary["meetings"], 1)

    def test_image_filename_is_unique_sanitized_and_type_driven(self):
        first = _safe_image_filename("../../front room.exe", "image/jpeg")
        second = _safe_image_filename("../../front room.exe", "image/jpeg")
        self.assertNotEqual(first, second)
        self.assertTrue(first.endswith("-front-room.jpg"))
        self.assertNotIn("..", first)

    @patch("broker.upload_handler.find_nearby_pois")
    @patch("broker.upload_handler.geocode_address")
    def test_enrichment_replaces_stale_location_data(self, geocode, nearby):
        geocode.return_value = (26.85, 80.95)
        nearby.return_value = {
            "metro_distance_km": 1.2,
            "hospital_distance_km": 0.8,
            "school_distance_km": 0.4,
        }
        prop = {
            "doc_id": "property-1",
            "metadata": {"raw_full_address": "Gomti Nagar"},
            "location": {"city": "Lucknow", "state": "Uttar Pradesh"},
            "connectivity": {"latitude": 1, "longitude": 2, "metro_distance_km": 99},
        }

        result = enrich_property(prop)

        self.assertEqual(result["connectivity"]["latitude"], 26.85)
        self.assertEqual(result["connectivity"]["longitude"], 80.95)
        self.assertEqual(result["connectivity"]["metro_distance_km"], 1.2)
        self.assertEqual(result["connectivity"]["status"], "enriched")

    @patch("broker.upload_handler.geocode_address", return_value=None)
    def test_failed_regeocode_does_not_keep_old_coordinates(self, geocode):
        prop = {
            "doc_id": "property-1",
            "metadata": {"raw_full_address": "Unknown address"},
            "location": {"city": "Lucknow", "state": "Uttar Pradesh"},
            "connectivity": {"latitude": 1, "longitude": 2},
        }

        result = enrich_property(prop)

        self.assertEqual(result["connectivity"], {"status": "pending_enrichment"})


if __name__ == "__main__":
    unittest.main()


class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeTable:
    def __init__(self, data):
        self.data = data

    def select(self, *args, **kwargs):
        return self

    def execute(self):
        return _FakeResult(self.data)


class _FakeAnalyticsClient:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return _FakeTable(self.tables[name])
