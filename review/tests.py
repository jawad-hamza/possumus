import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.core.cache import cache
from django.test import Client, override_settings
from django.urls import reverse

from pos.services.access import CASHIER_ROLE, assign_role
from pos.test_support import AuthenticatedAdminTestCase
from review import services
from review.models import ReviewNote, ReviewSwitch

AJAX = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest", "HTTP_ACCEPT": "application/json"}
PASSWORD = "CashierPass123!"


def anchor(**extra):
    data = {"selector": "#content>div:nth-of-type(2)", "tag": "td", "hint": "Soap 100g", "xFrac": 0.25,
            "yFrac": 0.5, "pageX": 120, "pageY": 340, "scope": "content"}
    data.update(extra)
    return data


@override_settings(REVIEW_NOTES_ENABLED=True)
class ReviewCase(AuthenticatedAdminTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)

    def cashier(self, username="till"):
        user = get_user_model().objects.create_user(username=username, password=PASSWORD, first_name="Till", last_name=username.title())
        assign_role(user, CASHIER_ROLE)
        return user

    def post_note(self, client=None, **payload):
        body = {"author": "", "text": "Make this column wider", "path": "/products/", "query": "",
                "screen": "Products", "anchor": anchor()}
        body.update(payload)
        return (client or self.client).post(
            reverse("review_notes_api"), data=json.dumps(body), content_type="application/json", **AJAX
        )

    def list_notes(self, path="/products/", query="", client=None):
        return (client or self.client).get(reverse("review_notes_api"), {"path": path, "query": query}, **AJAX)

    def note(self, user=None, **fields):
        values = {"author_name": "Someone", "text": "A note", "page_path": "/products/", "anchor": anchor(), "user": user}
        values.update(fields)
        return ReviewNote.objects.create(**values)


class NotesApiTests(ReviewCase):
    def test_create_then_list_only_on_the_same_screen(self):
        created = self.post_note(query="b=2&a=1", screen="Products - Awan Traders")
        self.assertEqual(created.status_code, 201)
        note = created.json()["note"]
        self.assertEqual(note["author"], "System Admin")  # blank name: the signed-in user's own
        self.assertEqual(note["query"], "a=1&b=2")
        self.assertEqual(note["url"], "/products/?a=1&b=2")
        self.assertTrue(note["canResolve"])
        self.assertEqual(ReviewNote.objects.get().user, self.admin_user)

        same = self.list_notes(query="a=1&b=2").json()["notes"]
        self.assertEqual([n["id"] for n in same], [note["id"]])
        # The same filters in another order are the same screen; other filters or pages are not.
        self.assertEqual(len(self.list_notes(query="?b=2&a=1").json()["notes"]), 1)
        self.assertEqual(self.list_notes(query="").json()["notes"], [])
        self.assertEqual(self.list_notes(path="/dashboard/", query="a=1&b=2").json()["notes"], [])

    def test_author_name_is_editable_and_anchor_is_cleaned(self):
        response = self.post_note(author="  Tester   One ", anchor=anchor(xFrac=7, tag="td onclick", junk="x"))
        self.assertEqual(response.status_code, 201)
        note = ReviewNote.objects.get()
        self.assertEqual(note.author_name, "Tester One")
        self.assertEqual(note.anchor["xFrac"], 1)
        self.assertEqual(note.anchor["tag"], "")
        self.assertNotIn("junk", note.anchor)
        self.assertEqual(note.anchor["hint"], "Soap 100g")

    def test_bad_notes_are_refused(self):
        self.assertEqual(self.post_note(text="   ").status_code, 400)
        self.assertEqual(self.post_note(text="x" * (services.TEXT_MAX + 1)).status_code, 400)
        self.assertEqual(self.post_note(path="https://evil.example/").status_code, 400)
        self.assertEqual(self.post_note(path="//evil.example/").status_code, 400)
        self.assertEqual(self.post_note(anchor="nope").status_code, 400)
        self.assertEqual(self.list_notes(path="").status_code, 400)
        self.assertFalse(ReviewNote.objects.exists())

    def test_sign_in_is_required(self):
        self.client.logout()
        self.assertEqual(self.post_note().status_code, 401)
        self.assertEqual(self.list_notes().status_code, 401)
        note = self.note()
        self.assertEqual(self.client.get(reverse("review_note_api", args=[note.id]), **AJAX).status_code, 401)
        resolve = self.client.post(reverse("review_note_resolve_api", args=[note.id]), data="{}", content_type="application/json", **AJAX)
        self.assertEqual(resolve.status_code, 401)
        # A plain browser visit is sent to the sign-in page instead.
        self.assertEqual(self.client.get(reverse("review_notes_api"), {"path": "/"}).status_code, 302)
        self.assertEqual(ReviewNote.objects.count(), 1)

    def test_csrf_token_is_required_to_post(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin_user)
        refused = self.post_note(client=client)
        self.assertEqual(refused.status_code, 403)
        self.assertFalse(ReviewNote.objects.exists())

        client.get(reverse("review_notes"))  # any page hands out the token, as the tool reads it
        token = client.cookies["csrftoken"].value
        accepted = client.post(
            reverse("review_notes_api"),
            data=json.dumps({"text": "With a token", "path": "/products/", "anchor": anchor()}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
            **AJAX,
        )
        self.assertEqual(accepted.status_code, 201)

    @override_settings(REVIEW_NOTES_ENABLED=False)
    def test_switched_off_tool_takes_no_notes(self):
        self.assertEqual(self.post_note().status_code, 403)
        self.assertEqual(self.list_notes().status_code, 403)
        self.assertFalse(ReviewNote.objects.exists())

        ReviewSwitch.set_mode(ReviewSwitch.MODE_ON, self.admin_user)
        self.assertEqual(self.post_note().status_code, 201)

    def test_owner_switch_can_turn_the_server_default_off(self):
        ReviewSwitch.set_mode(ReviewSwitch.MODE_OFF)
        self.assertEqual(self.post_note().status_code, 403)
        ReviewSwitch.set_mode(ReviewSwitch.MODE_SERVER)
        self.assertEqual(self.post_note().status_code, 201)

    def test_creating_notes_is_throttled(self):
        with mock.patch.object(services, "CREATE_LIMIT", (2, 600)):
            self.assertEqual(self.post_note().status_code, 201)
            self.assertEqual(self.post_note().status_code, 201)
            self.assertEqual(self.post_note().status_code, 429)
        self.assertEqual(ReviewNote.objects.count(), 2)

    def test_detail_returns_a_resolved_note_for_linked_opening(self):
        note = self.note(resolved=True)
        data = self.client.get(reverse("review_note_api", args=[note.id]), **AJAX).json()["note"]
        self.assertTrue(data["resolved"])
        self.assertEqual(self.client.get(reverse("review_note_api", args=[999999]), **AJAX).status_code, 404)


class ResolvePermissionTests(ReviewCase):
    def resolve(self, note, resolved=True):
        return self.client.post(
            reverse("review_note_resolve_api", args=[note.id]),
            data=json.dumps({"resolved": resolved}),
            content_type="application/json",
            **AJAX,
        )

    def test_author_and_admin_may_resolve_others_may_not(self):
        author = self.cashier("author")
        stranger = self.cashier("stranger")
        note = self.note(user=author)

        self.client.force_login(stranger)
        self.assertEqual(self.resolve(note).status_code, 403)
        listed = self.list_notes().json()["notes"]
        self.assertFalse(listed[0]["canResolve"])
        note.refresh_from_db()
        self.assertFalse(note.resolved)

        self.client.force_login(author)
        self.assertTrue(self.list_notes().json()["notes"][0]["canResolve"])
        response = self.resolve(note)
        self.assertEqual(response.status_code, 200)
        note.refresh_from_db()
        self.assertTrue(note.resolved)
        self.assertEqual(note.resolved_by, author)
        self.assertIsNotNone(note.resolved_at)
        # Resolved notes leave the screen.
        self.assertEqual(self.list_notes().json()["notes"], [])

        self.client.force_login(self.admin_user)
        self.assertEqual(self.resolve(note, resolved=False).status_code, 200)
        note.refresh_from_db()
        self.assertFalse(note.resolved)
        self.assertIsNone(note.resolved_by)
        self.assertEqual(len(self.list_notes().json()["notes"]), 1)

    def test_a_note_without_a_user_is_resolved_by_admins_only(self):
        note = self.note(user=None)
        self.client.force_login(self.cashier())
        self.assertEqual(self.resolve(note).status_code, 403)
        self.client.force_login(self.admin_user)
        self.assertEqual(self.resolve(note).status_code, 200)


class ReviewPageTests(ReviewCase):
    def test_only_admins_open_the_review_page(self):
        self.assertEqual(self.client.get(reverse("review_notes")).status_code, 200)

        self.client.force_login(self.cashier())
        self.assertEqual(self.client.get(reverse("review_notes")).status_code, 302)
        self.assertEqual(self.client.post(reverse("review_notes"), {"action": "switch", "mode": "off"}).status_code, 302)
        self.assertEqual(ReviewSwitch.current_mode(), ReviewSwitch.MODE_SERVER)

        self.client.logout()
        response = self.client.get(reverse("review_notes"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_lists_newest_first_and_filters_open_and_resolved(self):
        older = self.note(text="Older open note", page_query="a=1")
        newer = self.note(text="Newer open note")
        done = self.note(text="Fixed already", resolved=True)

        page = self.client.get(reverse("review_notes")).content.decode()
        self.assertIn("Newer open note", page)
        self.assertNotIn("Fixed already", page)
        self.assertLess(page.index("Newer open note"), page.index("Older open note"))
        self.assertIn(f'href="/products/?a=1#fbnote={older.id}"', page)
        self.assertIn(f"#fbnote={newer.id}", page)

        resolved = self.client.get(reverse("review_notes"), {"status": "resolved"}).content.decode()
        self.assertIn("Fixed already", resolved)
        self.assertNotIn("Newer open note", resolved)
        everything = self.client.get(reverse("review_notes"), {"status": "all"}).content.decode()
        for text in ("Older open note", "Newer open note", "Fixed already"):
            self.assertIn(text, everything)
        self.assertTrue(done.resolved)

    def test_resolve_and_reopen_from_the_page(self):
        note = self.note()
        response = self.client.post(reverse("review_notes"), {"action": "resolve", "note": note.id})
        self.assertEqual(response.status_code, 302)
        note.refresh_from_db()
        self.assertTrue(note.resolved)
        self.assertEqual(note.resolved_by, self.admin_user)
        self.client.post(reverse("review_notes"), {"action": "reopen", "note": note.id})
        note.refresh_from_db()
        self.assertFalse(note.resolved)

    def test_csv_export(self):
        self.note(text="=HYPERLINK(1)", author_name="Ali")
        response = self.client.get(reverse("review_notes"), {"status": "all", "format": "csv"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        body = response.content.decode("utf-8-sig")
        self.assertIn("Author", body.splitlines()[0])
        self.assertIn("Ali", body)
        self.assertIn("'=HYPERLINK(1)", body)  # never run as a formula

    def test_owner_switch(self):
        self.client.post(reverse("review_notes"), {"action": "switch", "mode": "off"})
        self.assertEqual(ReviewSwitch.current_mode(), ReviewSwitch.MODE_OFF)
        self.assertEqual(services.tool_state(), (False, ReviewSwitch.MODE_OFF))
        self.client.post(reverse("review_notes"), {"action": "switch", "mode": "bogus"})
        self.assertEqual(ReviewSwitch.current_mode(), ReviewSwitch.MODE_OFF)


class ToolRenderingTests(ReviewCase):
    TOOL_MARK = 'id="rv-config"'

    def test_tool_renders_only_when_enabled(self):
        page = self.client.get(reverse("review_notes")).content.decode()
        self.assertIn(self.TOOL_MARK, page)
        self.assertIn("review/review.js", page)
        self.assertIn("review/review.css", page)

        with override_settings(REVIEW_NOTES_ENABLED=False):
            page = self.client.get(reverse("review_notes")).content.decode()
        self.assertNotIn(self.TOOL_MARK, page)
        self.assertNotIn("review/review.js", page)

        ReviewSwitch.set_mode(ReviewSwitch.MODE_OFF)
        self.assertNotIn(self.TOOL_MARK, self.client.get(reverse("review_notes")).content.decode())

    def test_tool_config_carries_the_signed_in_user(self):
        response = self.client.get(reverse("review_notes"))
        config = response.context["review_tool"]()
        self.assertEqual(config["author"], "System Admin")
        self.assertEqual(config["userId"], self.admin_user.pk)
        self.assertTrue(config["canManage"])
        self.assertEqual(config["listUrl"], reverse("review_notes_api"))

    def test_never_on_the_sign_in_page(self):
        self.client.logout()
        page = self.client.get(reverse("login")).content.decode()
        self.assertNotIn(self.TOOL_MARK, page)
        self.assertNotIn("review/review.js", page)

    def test_print_css_hides_the_tool(self):
        path = finders.find("review/review.css")
        self.assertIsNotNone(path)
        with open(path, encoding="utf-8") as handle:
            css = handle.read()
        printed = css[css.index("@media print"):]
        self.assertIn("#rv-root", printed)
        self.assertIn("display: none !important", printed)

    def test_settings_page_links_to_the_review_page(self):
        page = self.client.get(reverse("settings_page")).content.decode()
        self.assertIn(reverse("review_notes"), page)
