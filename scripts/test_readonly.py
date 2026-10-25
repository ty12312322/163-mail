"""Read-only mailbox selection regressions. No credentials or network needed."""
import unittest
import mail163

MESSAGE = (
    b"From: sender@example.com\r\nTo: reader@example.com\r\n"
    b"Subject: Example reply\r\n\r\nExample body\r\n"
)

class RecordingIMAP:
    def __init__(self):
        self.selections = []
        self.fetches = []

    def select(self, mailbox, readonly=False):
        self.selections.append((mailbox, readonly))
        return "OK", [b"1"]

    def uid(self, command, *args):
        if command == "SEARCH":
            return "OK", [b"1"]
        if command == "FETCH":
            self.fetches.append(args)
            return "OK", [(b"1 (UID 1 BODY[] {1}", MESSAGE), b")"]
        raise AssertionError("Unexpected command: " + command)

class ReadOnlyTests(unittest.TestCase):
    def test_read_does_not_select_writable_mailbox(self):
        client = RecordingIMAP()
        mail163.fetch_message(client, 1)
        self.assertEqual(client.selections, [("INBOX", True)])
        self.assertIn("BODY.PEEK[]", client.fetches[0][1])

    def test_list_does_not_select_writable_mailbox(self):
        client = RecordingIMAP()
        mail163.list_headers(client)
        self.assertEqual(client.selections, [("INBOX", True)])

    def test_search_does_not_select_writable_mailbox(self):
        client = RecordingIMAP()
        mail163.search_messages(client)
        self.assertEqual(client.selections, [("INBOX", True)])

    def test_mark_read_is_an_explicit_opt_in(self):
        client = RecordingIMAP()
        mail163.fetch_message(client, 1, mark_read=True)
        self.assertEqual(client.selections, [("INBOX", False)])
        self.assertIn("BODY[]", client.fetches[0][1])

if __name__ == "__main__":
    unittest.main()
