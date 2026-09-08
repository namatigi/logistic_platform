from django.test import TestCase
from django.urls import reverse

from .models import Company
from users.models import CustomUser


class CompanyAgentRestrictionTest(TestCase):
    def setUp(self):
        self.agent = CustomUser.objects.create_user(
            email='agent@example.com', password='pass1234', role=CustomUser.Role.AGENT,
        )
        self.user = CustomUser.objects.create_user(
            email='shipper@example.com', password='pass1234',
        )

    def _login(self, email):
        self.client.login(email=email, password='pass1234')

    def test_agent_api_create_blocked(self):
        self._login('agent@example.com')
        response = self.client.post(reverse('companies:api_create'), {'name': 'Agent Co'}, content_type='application/json')
        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.json()['ok'])
        self.assertFalse(Company.objects.filter(name='Agent Co').exists())

    def test_agent_api_list_blocked(self):
        self._login('agent@example.com')
        response = self.client.get(reverse('companies:api_list'))
        self.assertEqual(response.status_code, 403)

    def test_agent_pages_blocked(self):
        self._login('agent@example.com')
        self.assertEqual(self.client.get(reverse('companies:list')).status_code, 403)
        self.assertEqual(self.client.get(reverse('companies:create')).status_code, 403)

    def test_regular_user_can_create_company(self):
        self._login('shipper@example.com')
        response = self.client.post(reverse('companies:api_create'), {'name': 'Shipper Co'}, content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        company = Company.objects.get(name='Shipper Co')
        self.assertEqual(company.user, self.user)

    def test_company_tin_vat_saved_and_returned(self):
        self._login('shipper@example.com')
        response = self.client.post(
            reverse('companies:api_create'),
            {'name': 'Shipper Co', 'registration_number': 'REG-1', 'tin': 'TIN-111', 'vat': 'VAT-222'},
            content_type='application/json',
        )
        self.assertTrue(response.json()['ok'])
        company = Company.objects.get(name='Shipper Co')
        self.assertEqual(company.tin, 'TIN-111')
        self.assertEqual(company.vat, 'VAT-222')
        body = self.client.get(reverse('companies:api_list')).json()
        saved = body['companies'][0]
        self.assertEqual(saved['tin'], 'TIN-111')
        self.assertEqual(saved['vat'], 'VAT-222')

    def test_agent_profile_hides_companies_card(self):
        self._login('agent@example.com')
        html = self.client.get(reverse('users:profile')).content.decode()
        self.assertNotIn('card-title">Companies', html)
        self.assertNotIn('data-show-companies', html)

    def test_regular_user_profile_shows_companies_card(self):
        self._login('shipper@example.com')
        html = self.client.get(reverse('users:profile')).content.decode()
        self.assertIn('card-title">Companies', html)
        self.assertIn('data-show-companies', html)