"""InProcessSession: Nautobot's API views, called in-process as a user, with that user's permissions."""

from __future__ import annotations

from django.contrib.contenttypes.models import ContentType
from nautobot.core.testing import TestCase
from nautobot.dcim.models import Location, LocationType
from nautobot.extras.models import Status
from nautobot.users.models import ObjectPermission, User

from nautobot_railyard.inprocess import BASE_URL, InProcessSession


class InProcessSessionTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        site = LocationType.objects.create(name="Site")
        active = Status.objects.get(name="Active")
        for name in ("LDN1", "MAN1"):
            Location.objects.create(name=name, location_type=site, status=active)
        cls.admin = User.objects.create(username="admin-user", is_superuser=True, is_active=True)
        cls.limited = User.objects.create(username="limited-user", is_active=True)
        permission = ObjectPermission.objects.create(name="ldn1 only", actions=["view"], constraints={"name": "LDN1"})
        permission.users.add(cls.limited)
        permission.object_types.add(ContentType.objects.get_by_natural_key("dcim", "location"))

    def test_status_and_lists_as_json(self):
        session = InProcessSession(self.admin)
        response = session.request("GET", f"{BASE_URL}/api/status/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("nautobot-version", response.json())
        listed = session.request("GET", f"{BASE_URL}/api/dcim/locations/", params=[("depth", 1), ("limit", 1)])
        page = listed.json()
        self.assertEqual(page["count"], 2)
        self.assertEqual(len(page["results"]), 1)
        self.assertEqual(page["results"][0]["location_type"]["name"], "Site")

    def test_the_users_object_permissions_apply(self):
        page = InProcessSession(self.limited).request("GET", f"{BASE_URL}/api/dcim/locations/").json()
        self.assertEqual([r["name"] for r in page["results"]], ["LDN1"])
        refused = InProcessSession(self.limited).request(
            "POST", f"{BASE_URL}/api/dcim/manufacturers/", json={"name": "Acme"}
        )
        self.assertEqual(refused.status_code, 403)

    def test_writes_and_validation_errors(self):
        session = InProcessSession(self.admin)
        created = session.request("POST", f"{BASE_URL}/api/dcim/manufacturers/", json={"name": "Acme"})
        self.assertEqual(created.status_code, 201)
        duplicate = session.request("POST", f"{BASE_URL}/api/dcim/manufacturers/", json={"name": "Acme"})
        self.assertEqual(duplicate.status_code, 400)
        self.assertIn("name", duplicate.json())

    def test_an_unknown_path_is_a_404(self):
        response = InProcessSession(self.admin).request("GET", f"{BASE_URL}/api/nowhere/")
        self.assertEqual(response.status_code, 404)

    def test_an_inactive_user_is_refused(self):
        self.limited.is_active = False
        with self.assertRaises(ValueError):
            InProcessSession(self.limited)
