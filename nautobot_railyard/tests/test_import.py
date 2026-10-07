"""The Import into Railyard job against a real Nautobot database: a location becomes a Railyard estate, a refresh
merges by Nautobot's UUIDs, conflicts and Railyard's check stop the save, and the user's permissions apply."""

from __future__ import annotations

from django.contrib.contenttypes.models import ContentType
from django.test import override_settings
from nautobot.dcim.models import Cable, Device, DeviceType, Interface, Location, LocationType, Manufacturer, Rack
from nautobot.extras.models import Role, Status
from nautobot.users.models import User

from nautobot_railyard import policy

from .base import RailyardJobTestCase, Response, app_settings

IMPORT = "ImportIntoRailyard"


def content_types(*labels: str):
    return [ContentType.objects.get_by_natural_key(*label.split(".")) for label in labels]


class ImportJobTest(RailyardJobTestCase):
    def setUp(self):
        super().setUp()
        cls = self
        active = Status.objects.get(name="Active")
        region = LocationType.objects.create(name="Region", nestable=True)
        site = LocationType.objects.create(name="Site", parent=region)
        room = LocationType.objects.create(name="Room", parent=site)
        for t in (site, room):
            t.content_types.set(content_types("dcim.rack", "dcim.device"))
        europe = Location.objects.create(name="Europe", location_type=region, status=active)
        cls.ldn1 = Location.objects.create(name="LDN1", location_type=site, parent=europe, status=active)
        cls.hall = Location.objects.create(name="Hall 1", location_type=room, parent=cls.ldn1, status=active)
        Location.objects.create(name="MAN1", location_type=site, parent=europe, status=active)
        role = Role.objects.create(name="Leaf")
        role.content_types.set(content_types("dcim.device"))
        maker = Manufacturer.objects.create(name="Arista")
        switch = DeviceType.objects.create(manufacturer=maker, model="DCS-7050", u_height=1)
        rack = Rack.objects.create(name="A01", location=cls.hall, status=active, u_height=42)
        leaf1 = Device.objects.create(
            name="leaf1",
            device_type=switch,
            role=role,
            location=cls.hall,
            rack=rack,
            position=40,
            face="front",
            status=active,
        )
        leaf2 = Device.objects.create(
            name="leaf2",
            device_type=switch,
            role=role,
            location=cls.hall,
            rack=rack,
            position=39,
            face="front",
            status=active,
        )
        a = Interface.objects.create(device=leaf1, name="Ethernet1", type="10gbase-x-sfpp", status=active)
        b = Interface.objects.create(device=leaf2, name="Ethernet1", type="10gbase-x-sfpp", status=active)
        Cable.objects.create(
            termination_a=a, termination_b=b, status=Status.objects.get(name="Connected"), type="dac-passive"
        )

    def import_(self, username: str = "railyard-admin", **kwargs):
        kwargs.setdefault("locations", [str(self.ldn1.pk)])
        kwargs.setdefault("dryrun", False)
        if "project" not in kwargs:
            kwargs.setdefault("new_project", "LDN1 baseline")
        return self.run_job(IMPORT, username=username, **kwargs)

    def test_a_dry_run_saves_nothing(self):
        result = self.import_(dryrun=True)
        self.assertSucceeded(result)
        self.assertEqual(self.railyard.writes(), [])
        log = self.log(result)
        self.assertIn("Read Nautobot (LDN1): 1 rack, 2 devices, 1 cable, 0 power links.", log)
        self.assertIn("Dry run: would create 'LDN1 baseline'. Nothing was saved to Railyard.", log)

    def test_a_location_becomes_an_estate_and_a_refresh_merges_it(self):
        result = self.import_()
        self.assertSucceeded(result)
        [put] = self.railyard.writes()
        project = put["json"]
        self.assertEqual(project["name"], "LDN1 baseline")
        containers = {c["id"]: c for c in project["containers"]}
        site = containers[f"nbt-site-{self.ldn1.pk}"]
        self.assertEqual((site["type"], site["layout"]), ("Site", "floor"))
        self.assertEqual(containers[f"nbt-loc-{self.hall.pk}"]["type"], "Room")
        [rack] = project["racks"]
        self.assertEqual(rack["containerId"], f"nbt-loc-{self.hall.pk}")
        self.assertEqual({p["label"] for p in rack["placements"]}, {"leaf1", "leaf2"})
        self.assertEqual(len(project["cables"]), 1)
        sync = project["meta"]["railyardSync"]
        self.assertEqual(
            (sync["source"], sync["url"], sync["prefix"]), ("nautobot", "https://nautobot.example.com", "nbt")
        )
        self.assertEqual(result.result["revision"], 8)
        self.assertIn("Created 'LDN1 baseline'", self.log(result))

        # Nothing changed in Nautobot: nothing is saved.
        again = self.import_(project=project["id"])
        self.assertSucceeded(again)
        self.assertEqual(len(self.railyard.writes()), 1)
        self.assertIn("is already up to date with Nautobot; nothing was saved.", self.log(again))

        # A rename in Nautobot updates the estate in place, onto the revision it merged.
        Device.objects.filter(name="leaf2").update(name="leaf2-renamed")
        renamed = self.import_(project=project["id"])
        self.assertSucceeded(renamed)
        put = self.railyard.writes()[-1]
        self.assertEqual(put["headers"].get("If-Match"), '"8"')
        labels = {p["label"] for p in put["json"]["racks"][0]["placements"]}
        self.assertEqual(labels, {"leaf1", "leaf2-renamed"})

    def test_conflicts_with_the_railyard_design_stop_the_save(self):
        self.assertSucceeded(self.import_())
        estate = self.railyard.project
        # In Railyard, someone puts a device at U30; in Nautobot, leaf2 moves there.
        estate["racks"][0]["placements"].append(
            {"id": "mine", "startU": 30, "heightU": 1, "face": "front", "label": "mine"}
        )
        Device.objects.filter(name="leaf2").update(position=30)
        result = self.import_(project=estate["id"])
        self.assertFailed(result, "conflict(s) between Railyard's design and Nautobot")
        self.assertEqual(len(self.railyard.writes()), 1)

    def test_refreshing_from_fewer_locations_is_refused(self):
        man1 = Location.objects.get(name="MAN1")
        self.assertSucceeded(self.import_(locations=[str(self.ldn1.pk), str(man1.pk)]))
        result = self.import_(project=self.railyard.project["id"])
        self.assertFailed(result, "import all of them again")

    def test_a_plan_limit_is_explained(self):
        self.railyard.put_response = Response(
            402,
            {
                "error": "rack limit",
                "code": "plan_limit",
                "plan": "community",
                "resource": "racks",
                "limit": 0,
                "current": 1,
                "scope": "estate",
                "requiredPlans": ["pro"],
                "projectPass": True,
            },
        )
        result = self.import_()
        self.assertFailed(result, "This import has 1 racks; the Community plan allows 0 per estate.")

    def test_the_user_needs_view_permissions(self):
        user = User.objects.create(username="nobody", is_active=True)
        self.grant(user, ["run"], ["extras.job"])
        result = self.import_(username="nobody")
        self.assertFailed(result, "nobody lacks the Nautobot permissions this job needs: dcim.view_cable")
        self.assertEqual(self.railyard.calls, [])

    def test_the_import_reads_only_what_the_user_may_see(self):
        user = User.objects.create(username="partial", is_active=True)
        labels = [f"{a}.{m}" for a, m in policy.IMPORT_MODELS if m != "device"]
        self.grant(user, ["view"], labels)
        self.grant(user, ["view"], ["dcim.device"], constraints={"name": "leaf1"})
        self.grant(user, ["run"], ["extras.job"])
        result = self.import_(username="partial")
        self.assertSucceeded(result)
        labels = {p["label"] for p in self.railyard.writes()[0]["json"]["racks"][0]["placements"]}
        self.assertEqual(labels, {"leaf1"})

    def test_a_project_allowlist_allows_refreshes_only(self):
        with override_settings(PLUGINS_CONFIG=app_settings(railyard_allowed_projects=["ldn1-baseline"])):
            result = self.import_()
        self.assertFailed(result, "can refresh one of them but not create a new estate")

    def test_choose_locations_or_all_and_refresh_or_create(self):
        self.assertFailed(self.import_(locations=[], all_locations=False), "Choose the locations to import")
        self.assertFailed(
            self.import_(project="x", new_project="y"), "Name the estate to refresh (Refresh project) or a new one"
        )
        result = self.import_(locations=[], all_locations=True, dryrun=True)
        self.assertSucceeded(result)
        self.assertIn("Read Nautobot (LDN1, MAN1)", self.log(result))
