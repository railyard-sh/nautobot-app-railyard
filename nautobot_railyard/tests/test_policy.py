"""The Nautobot-free policy: the org/project allowlist, token sources and the permissions each job needs. Plain
``unittest`` cases, so they run under Nautobot's test runner and on their own (``python -m unittest``)."""

from __future__ import annotations

import os
import tempfile
import unittest

from nautobot_railyard import policy
from nautobot_railyard.policy import SyncTargets, TargetNotAllowed, read_token


class SyncTargetsTest(unittest.TestCase):
    def test_the_default_org_and_further_allowed_ones(self):
        targets = SyncTargets.from_settings(default_org="acme", allowed_orgs="Other, ACME ,x", allowed_projects=None)
        self.assertEqual(targets.orgs, ("acme", "Other", "x"))
        self.assertEqual(targets.check_org(None), "acme")
        self.assertEqual(targets.check_org("other"), "Other")
        with self.assertRaises(TargetNotAllowed):
            targets.check_org("evil")

    def test_no_org_configured_means_the_personal_org_only(self):
        targets = SyncTargets.from_settings(default_org=None, allowed_orgs=[], allowed_projects=[])
        self.assertIsNone(targets.check_org(""))
        with self.assertRaises(TargetNotAllowed):
            targets.check_org("acme")

    def test_projects(self):
        open_ = SyncTargets.from_settings(default_org="acme", allowed_orgs=None, allowed_projects=None)
        self.assertEqual(open_.check_project(" ldn1-design "), "ldn1-design")
        open_.check_new_project()
        with self.assertRaises(TargetNotAllowed):
            open_.check_project("../etc")
        closed = SyncTargets.from_settings(default_org="acme", allowed_orgs=None, allowed_projects=["LDN1-design"])
        self.assertEqual(closed.check_project("ldn1-design"), "LDN1-design")
        with self.assertRaises(TargetNotAllowed):
            closed.check_project("other")
        with self.assertRaises(TargetNotAllowed):
            closed.check_new_project()


class TokenTest(unittest.TestCase):
    def test_sources_in_order(self):
        self.assertEqual(read_token(secret=lambda: " s3cret ", token_file="/nope", token="x"), "s3cret")
        with tempfile.NamedTemporaryFile("w", delete=False) as fh:
            fh.write("from-file\n")
        self.addCleanup(os.unlink, fh.name)
        self.assertEqual(read_token(token_file=fh.name, token_env="T", environ={"T": "env"}), "from-file")
        self.assertEqual(read_token(token_env="T", environ={"T": " env "}, token="literal"), "env")
        self.assertEqual(read_token(token_env="T", environ={}, token=" literal "), "literal")
        self.assertEqual(read_token(token_env=None, environ={"T": "env"}), "")

    def test_an_unreadable_file_says_which(self):
        with self.assertRaisesRegex(ValueError, "railyard_token_file '/no/such/file'"):
            read_token(token_file="/no/such/file")


class PermissionsTest(unittest.TestCase):
    def test_export_permissions_grow_with_the_options(self):
        dry = policy.export_permissions(dry_run=True, allow_deletes=True)
        self.assertIn("dcim.view_device", dry)
        self.assertIn("extras.view_role", dry)
        self.assertNotIn("dcim.add_device", dry)
        apply = policy.export_permissions(dry_run=False, allow_deletes=False)
        self.assertTrue({"dcim.add_cable", "dcim.change_locationtype", "extras.add_status"} <= apply)
        self.assertNotIn("dcim.delete_device", apply)
        self.assertIn("dcim.delete_device", policy.export_permissions(dry_run=False, allow_deletes=True))

    def test_import_needs_view_only(self):
        perms = policy.import_permissions()
        self.assertIn("dcim.view_powerfeed", perms)
        self.assertTrue(all(".view_" in p for p in perms))

    def test_missing(self):
        held = {"dcim.view_device"}
        self.assertEqual(policy.missing(held.__contains__, ["dcim.view_rack", "dcim.view_device"]), ["dcim.view_rack"])


if __name__ == "__main__":
    unittest.main()
