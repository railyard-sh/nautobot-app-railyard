"""The Export from Railyard job against a real Nautobot database: ownership, dry runs, all-or-nothing applies,
permissions, the allowlist and Railyard's plan refusals."""

from __future__ import annotations

import copy

from django.contrib.contenttypes.models import ContentType
from django.test import override_settings
from nautobot.dcim.models import (
    Cable,
    Device,
    DeviceType,
    FrontPort,
    Interface,
    Location,
    LocationType,
    Manufacturer,
    Rack,
    RearPort,
)
from nautobot.extras.models import CustomField, ObjectChange, Role, Status, Tag
from nautobot.ipam.models import IPAddress, IPAddressToInterface, Namespace, Prefix
from nautobot.users.models import User
from railyard_sync.export.policy import ownership_tag

from nautobot_railyard import policy

from .base import DOCUMENT, RAILYARD, RailyardJobTestCase, Response, app_settings

EXPORT = "ExportFromRailyard"
COUNTS = {Location: 3, DeviceType: 4, Rack: 1, Device: 4, Cable: 3}  # all there is (the org models: by owner)
OWNED_ORG_MODELS = {LocationType: 3, Manufacturer: 1, Role: 4}


def spec(doc: dict = DOCUMENT):
    return ownership_tag(RAILYARD, doc["project"]["id"], doc["project"]["name"])


def without_device(doc: dict, name: str) -> dict:
    doc = copy.deepcopy(doc)
    objects = doc["objects"]
    objects["devices"] = [r for r in objects["devices"] if r["name"] != name]
    for kind in ("interfaces", "rear-ports", "front-ports", "power-outlets", "power-ports"):
        objects[kind] = [r for r in objects[kind] if r["device__name"] != name]
    objects["cables"] = [c for c in objects["cables"] if name not in (c["a"]["device"], c["b"]["device"])]
    return doc


class ExportJobTest(RailyardJobTestCase):
    def export(self, username: str = "railyard-admin", **kwargs):
        kwargs.setdefault("project", "cabled")
        kwargs.setdefault("dryrun", False)
        return self.run_job(EXPORT, username=username, **kwargs)

    def test_a_dry_run_writes_nothing(self):
        result = self.export(dryrun=True)
        self.assertSucceeded(result)
        self.assertEqual(Device.objects.count(), 0)
        self.assertFalse(Tag.objects.filter(name=spec().name).exists())
        self.assertFalse(CustomField.objects.filter(key__in=["railyard_id", "railyard_owner"]).exists())
        self.assertEqual(result.result["diff"]["create"], 32)
        log = self.log(result)
        self.assertIn("Dry run, planned: 32 to create", log)
        self.assertIn("Dry run: nothing was written to Nautobot.", log)
        self.assertIn("Fetching the Nautobot sync document", log)

    def test_apply_creates_owned_objects_and_a_second_run_changes_nothing(self):
        result = self.export()
        self.assertSucceeded(result)
        for model, n in COUNTS.items():
            self.assertEqual(model.objects.count(), n, model.__name__)
        for model, n in OWNED_ORG_MODELS.items():
            owned = model.objects.filter(_custom_field_data__railyard_owner=spec().slug)
            self.assertEqual(owned.count(), n, model.__name__)
        tag = Tag.objects.get(name=spec().name)
        self.assertEqual(tag.description, spec().description)
        self.assertEqual(Device.objects.filter(tags=tag).count(), 4)
        sw = Device.objects.get(name="SW-1")
        self.assertEqual(sw._custom_field_data["railyard_id"], "p_sw")
        self.assertEqual(sw.rack.name, "R1")
        self.assertEqual(sw.location.name, "A")
        self.assertEqual(sw.location.location_type.name, "Row")
        self.assertEqual(sw.location.parent.parent.name, "DC1")
        role = Role.objects.get(name="leaf")
        self.assertEqual(role._custom_field_data["railyard_owner"], spec().slug)
        self.assertEqual(list(role.content_types.values_list("model", flat=True)), ["device"])
        self.assertEqual(Rack.objects.get(name="R1").status.name, "Planned")
        self.assertEqual(Interface.objects.get(device=sw, name="Eth1").status.name, "Active")
        self.assertTrue(Cable.objects.filter(label="L1", color="ff0000", status__name="Connected").exists())
        # Change-logged as the user who ran the job.
        self.assertTrue(ObjectChange.objects.filter(changed_object_id=sw.pk, user__username="railyard-admin").exists())

        again = self.export()
        self.assertSucceeded(again)
        self.assertEqual(again.result["diff"], {"create": 0, "update": 0, "delete": 0, "no-change": 32})
        self.assertIn("Nautobot is in step with the Railyard estate.", self.log(again))

    def test_shared_objects_are_used_and_a_role_not_enabled_for_devices_is_a_conflict(self):
        Manufacturer.objects.create(name="Acme")
        leaf = Role.objects.create(name="leaf", color="aaaaaa")
        leaf.content_types.set([ContentType.objects.get_by_natural_key("dcim", "rack")])
        result = self.export()
        self.assertSucceeded(result)
        log = self.log(result)
        self.assertIn("manufacturer Acme (exists; used as-is, not modified)", log)
        self.assertIn("role leaf exists but isn't enabled for dcim.device", log)
        self.assertFalse(Device.objects.filter(name="SW-1").exists())
        self.assertEqual(Device.objects.count(), 3)
        self.assertEqual(Manufacturer.objects.get(name="Acme")._custom_field_data.get("railyard_owner"), None)

    def test_an_operators_device_of_the_same_name_is_left_alone(self):
        self.export()
        sw = Device.objects.get(name="SW-1")
        tag = Tag.objects.get(name=spec().name)
        sw.tags.remove(tag)  # now it is the operator's
        sw.comments = "mine"
        sw.save()
        result = self.export()
        self.assertSucceeded(result)
        self.assertIn(
            "device SW-1 in location A already exists there and is not managed by this sync", self.log(result)
        )
        sw.refresh_from_db()
        self.assertEqual(sw.comments, "mine")
        self.assertFalse(sw.tags.filter(pk=tag.pk).exists())

    def test_deletes_are_opt_in_and_guarded(self):
        self.export()
        DOC = without_device(DOCUMENT, "PP-1")
        self.railyard.deliverable = DOC
        kept = self.export()
        self.assertSucceeded(kept)
        self.assertTrue(Device.objects.filter(name="PP-1").exists())
        self.assertIn("Stale (", self.log(kept))

        # An operator's IP address on SW-1's interface keeps SW-1 (deleting it would unassign the address).
        namespace, _ = Namespace.objects.get_or_create(name="Global")
        Prefix.objects.create(prefix="10.0.0.0/24", namespace=namespace, status=Status.objects.get(name="Active"))
        ip = IPAddress.objects.create(
            address="10.0.0.1/24", namespace=namespace, status=Status.objects.get(name="Active")
        )
        eth1 = Interface.objects.get(device__name="SW-1", name="Eth1")
        IPAddressToInterface.objects.create(ip_address=ip, interface=eth1)
        self.railyard.deliverable = without_device(DOC, "SW-1")
        result = self.export(allow_deletes=True)
        self.assertSucceeded(result)
        self.assertFalse(Device.objects.filter(name="PP-1").exists())
        self.assertTrue(Interface.objects.filter(pk=eth1.pk).exists())
        self.assertIn("would modify 1 IP address assignment not managed by this sync", self.log(result))

    def test_a_refused_write_rolls_the_whole_export_back(self):
        doc = copy.deepcopy(DOCUMENT)
        doc["objects"]["racks"][0]["width"] = "17"  # Nautobot has no 17-inch racks
        self.railyard.deliverable = doc
        result = self.export()
        self.assertFailed(result, "so the export was rolled back: nothing was changed in Nautobot")
        self.assertEqual(Location.objects.count(), 0)
        self.assertFalse(Tag.objects.filter(name=spec().name).exists())

    def test_the_user_needs_the_permissions_to_write(self):
        user = User.objects.create(username="viewer", is_active=True)
        self.grant(user, ["view"], [f"{a}.{m}" for a, m in policy.EXPORT_MODELS] + ["extras.tag", "extras.customfield"])
        self.grant(user, ["run"], ["extras.job"])
        self.assertSucceeded(self.export(username="viewer", dryrun=True))
        result = self.export(username="viewer")
        self.assertFailed(result, "viewer lacks the Nautobot permissions this job needs: dcim.add_cable")
        self.assertEqual(Device.objects.count(), 0)

    def test_object_level_constraints_are_enforced_by_the_api(self):
        user = User.objects.create(username="limited", is_active=True)
        labels = [f"{a}.{m}" for a, m in policy.EXPORT_MODELS if m != "rack"] + ["extras.tag", "extras.customfield"]
        self.grant(user, ["view", "add", "change"], labels)
        self.grant(user, ["view", "add", "change"], ["dcim.rack"], constraints={"location__name": "Elsewhere"})
        self.grant(user, ["run"], ["extras.job"])
        result = self.export(username="limited")
        self.assertFailed(result, "rolled back")
        self.assertEqual(Rack.objects.count(), 0)
        self.assertEqual(Location.objects.count(), 0)

    def test_only_allowed_orgs_and_projects(self):
        with override_settings(PLUGINS_CONFIG=app_settings(railyard_allowed_projects=["other"])):
            result = self.export()
        self.assertFailed(result, "Project 'cabled' is not in this Nautobot's railyard_allowed_projects.")
        result = self.export(org="evil")
        self.assertFailed(result, "Organisation 'evil' is not in this Nautobot's railyard_allowed_orgs.")
        self.assertEqual(self.railyard.calls, [])  # Railyard was never asked

    def test_a_plan_without_deliverables_explains_the_upgrade(self):
        self.railyard.deliverable = Response(
            402,
            {
                "error": "no deliverables",
                "code": "plan_required",
                "deliverable": "nautobot-sync",
                "plan": "community",
                "requiredPlans": ["pro", "team"],
                "projectPass": True,
            },
        )
        result = self.export()
        self.assertFailed(
            result, "Exporting to Nautobot needs a plan with deliverables: the Community plan does not include them."
        )
        self.assertIn("Nothing was written to Nautobot.", self.log(result))

    def test_no_token(self):
        with override_settings(PLUGINS_CONFIG=app_settings(railyard_token="")):
            result = self.export()
        self.assertFailed(result, "No Railyard token")

    def test_the_token_never_reaches_the_log(self):
        from .base import TOKEN

        result = self.export(dryrun=True)
        self.assertNotIn(TOKEN, self.log(result))
        self.assertNotIn(TOKEN, str(result.result))

    def test_front_ports_swapped_in_railyard_are_remapped(self):
        self.assertSucceeded(self.export())
        doc = copy.deepcopy(DOCUMENT)
        for row in doc["objects"]["front-ports"]:
            row["rear_port__name"] = {"1": "2", "2": "1"}[row["rear_port__name"]]
        self.railyard.deliverable = doc
        result = self.export()
        self.assertSucceeded(result)
        mapped = {f.name: f.rear_port.name for f in FrontPort.objects.filter(device__name="PP-1")}
        self.assertEqual(mapped, {"1": "2", "2": "1"})
        self.assertEqual(set(RearPort.objects.filter(device__name="PP-1").values_list("positions", flat=True)), {1})

    def test_a_0u_device_type_stays_0u(self):
        self.assertSucceeded(self.export())
        self.assertEqual(DeviceType.objects.get(model="PDU1").u_height, 0)
        self.assertIsNone(Device.objects.get(name="PDU-1").position)
