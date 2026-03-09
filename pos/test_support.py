from django.contrib.auth import get_user_model
from django.test import TestCase

from pos.services.access import ADMIN_ROLE, assign_role, ensure_role_groups


class AuthenticatedAdminTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        ensure_role_groups()
        user_model = get_user_model()
        cls.admin_user = user_model.objects.create_user(
            username="admin",
            password="AdminPass123!",
            first_name="System",
            last_name="Admin",
            is_staff=True,
        )
        assign_role(cls.admin_user, ADMIN_ROLE)

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin_user)
