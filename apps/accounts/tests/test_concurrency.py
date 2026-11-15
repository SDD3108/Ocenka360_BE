"""Run on the PostgreSQL CI job; SQLite is only the local development backend."""

import io
import re
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless

from django.core import mail
from django.core.management import call_command
from django.db import close_old_connections, connection
from django.test import TransactionTestCase
from rest_framework.exceptions import AuthenticationFailed, Throttled

from apps.accounts.models import AuthSession
from apps.accounts.security import reserve_quota
from apps.accounts.services import InvalidCode, request_code, rotate_refresh, verify_code


@skipUnless(connection.vendor == "postgresql", "Concurrent locking verified in PostgreSQL CI only")
class PostgreSQLConcurrencyTests(TransactionTestCase):
    def setUp(self):
        call_command("seed_legal_drafts", stdout=io.StringIO())

    def run_parallel(self, function, workers=2):
        barrier = Barrier(workers)

        def work():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return function()
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(lambda _: work(), range(workers)))

    def challenge(self):
        challenge = request_code("parallel@example.com", "ru", "192.0.2.1")
        code = re.search(r"\b[0-9]{6}\b", mail.outbox[-1].body).group()
        return challenge, code

    def test_concurrent_quota_has_only_one_winner(self):
        def reserve():
            try:
                reserve_quota("parallel", "same-identity", 1, 3600)
                return "ok"
            except Throttled:
                return "limited"

        self.assertCountEqual(self.run_parallel(reserve), ["ok", "limited"])

    def test_code_consumption_has_only_one_winner(self):
        challenge, code = self.challenge()

        def verify():
            try:
                verify_code(challenge.pk, code, "192.0.2.1", "thread")
                return "ok"
            except InvalidCode:
                return "rejected"

        self.assertCountEqual(self.run_parallel(verify), ["ok", "rejected"])
        self.assertEqual(AuthSession.objects.count(), 1)

    def test_concurrent_refresh_detects_reuse_and_revokes_session(self):
        challenge, code = self.challenge()
        _, tokens, _ = verify_code(challenge.pk, code, "192.0.2.1", "thread")

        def refresh():
            try:
                rotate_refresh(tokens["refresh"])
                return "ok"
            except AuthenticationFailed:
                return "rejected"

        self.assertCountEqual(self.run_parallel(refresh), ["ok", "rejected"])
        self.assertIsNotNone(AuthSession.objects.get().revoked_at)
