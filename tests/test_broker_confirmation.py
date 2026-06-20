import unittest
from datetime import datetime
from unittest.mock import patch

from agent.broker_confirmation import ask_broker_availability, handle_broker_reply


class BrokerConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.when = datetime(2026, 6, 21, 17, 0)

    @patch("database.supabase_client.save_broker_confirmation")
    @patch("notifications.whatsapp_notifier._send", return_value=True)
    def test_availability_request_keeps_original_meeting_id(self, send, save_confirmation):
        sent = ask_broker_availability(
            buyer_name="Asha",
            buyer_phone="9876543210",
            buyer_session_id="wa_buyer",
            proposed_when="Sunday at 5 pm",
            proposed_dt=self.when,
            property_id="property-1",
            lead_id="lead-1",
            meeting_id="meeting-1",
            broker_phone="9876500000",
        )

        self.assertTrue(sent)
        payload = save_confirmation.call_args.args[0]
        self.assertEqual(payload["meeting_id"], "meeting-1")

    @patch("agent.property_agent._gcal_link", return_value="https://calendar.test/event")
    @patch("agent.broker_confirmation._send_broker_calendar_invite")
    @patch("database.supabase_client.update_lead")
    @patch("database.supabase_client.save_meeting")
    @patch("database.supabase_client.update_meeting")
    @patch("database.supabase_client.update_broker_confirmation")
    @patch("database.supabase_client.get_pending_broker_confirmation")
    @patch("notifications.whatsapp_notifier._send", return_value=True)
    def test_yes_updates_existing_meeting_without_duplicate(
        self, send, get_pending, update_confirmation, update_meeting,
        save_meeting, update_lead, broker_invite, gcal_link,
    ):
        get_pending.return_value = self._confirmation(status="pending")

        self.assertTrue(handle_broker_reply("9876500000", "YES"))

        update_meeting.assert_called_once()
        self.assertEqual(update_meeting.call_args.args[0], "meeting-1")
        self.assertEqual(update_meeting.call_args.args[1]["status"], "confirmed")
        save_meeting.assert_not_called()
        update_lead.assert_called_once_with("lead-1", {"status": "visit"})
        broker_invite.assert_called_once()

    @patch("database.supabase_client.update_meeting")
    @patch("database.supabase_client.update_broker_confirmation")
    @patch("database.supabase_client.get_pending_broker_confirmation")
    @patch("database.supabase_client.save_session")
    @patch("database.supabase_client.get_session", return_value={"messages": [], "requirements": {}})
    @patch("notifications.whatsapp_notifier._send", return_value=True)
    def test_no_cancels_pending_meeting(
        self, send, get_session, save_session, get_pending,
        update_confirmation, update_meeting,
    ):
        get_pending.return_value = self._confirmation(status="pending")

        self.assertTrue(handle_broker_reply("9876500000", "NO"))

        self.assertEqual(update_meeting.call_args.args[0], "meeting-1")
        self.assertEqual(update_meeting.call_args.args[1]["status"], "cancelled")
        self.assertEqual(save_session.call_args.args[0], "wa_919876543210")
        self.assertEqual(save_session.call_args.args[3], "scheduling")

    @patch("agent.property_agent._gcal_link", return_value="https://calendar.test/new")
    @patch("agent.property_agent._parse_visit_time")
    @patch("database.supabase_client.update_meeting")
    @patch("database.supabase_client.update_broker_confirmation")
    @patch("database.supabase_client.get_latest_broker_confirmation")
    @patch("database.supabase_client.get_pending_broker_confirmation")
    @patch("notifications.whatsapp_notifier._send", return_value=True)
    def test_reschedule_uses_latest_confirmed_visit(
        self, send, get_pending, get_latest, update_confirmation,
        update_meeting, parse_time, gcal_link,
    ):
        get_latest.return_value = self._confirmation(status="yes")
        parse_time.return_value = (self.when, "Sunday at 5 pm")

        self.assertTrue(handle_broker_reply("9876500000", "reschedule to Sunday 5pm"))

        get_pending.assert_not_called()
        update_meeting.assert_called_once()
        self.assertEqual(update_meeting.call_args.args[0], "meeting-1")
        self.assertEqual(update_meeting.call_args.args[1]["status"], "confirmed")
        update_confirmation.assert_called_once_with(
            "confirmation-1",
            "rescheduled",
            {"proposed_dt": self.when.isoformat(), "proposed_when": "Sunday at 5 pm"},
        )
        get_latest.assert_called_once_with("9876500000", None)

    def _confirmation(self, status):
        return {
            "id": "confirmation-1",
            "meeting_id": "meeting-1",
            "lead_id": "lead-1",
            "property_id": "property-1",
            "buyer_name": "Asha",
            "buyer_phone": "9876543210",
            "buyer_session_id": "",
            "proposed_dt": self.when.isoformat(),
            "proposed_when": "Sunday at 5 pm",
            "status": status,
        }


if __name__ == "__main__":
    unittest.main()
