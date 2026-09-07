from django.test import TestCase
from django.urls import reverse

from .models import Address, CustomUser, Profile


class ProfileViewsTest(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user(email='alice@example.com', password='pass1234')
        self.client.login(email='alice@example.com', password='pass1234')

    def test_profile_page_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse('users:profile'))
        self.assertEqual(response.status_code, 302)

    def test_profile_page_renders(self):
        response = self.client.get(reverse('users:profile'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'profile-app')

    def test_api_profile_returns_defaults(self):
        response = self.client.get(reverse('users:api_profile'))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['profile']['email'], 'alice@example.com')
        self.assertEqual(data['addresses'], [])

    def test_api_profile_save_bio_and_email(self):
        response = self.client.post(reverse('users:api_profile_save'), {
            'email': 'alice@example.com',
            'phone': '+1 555 0100',
            'bio': 'Freight coordinator',
        })
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['ok'])
        profile = Profile.objects.get(user=self.user)
        self.assertEqual(profile.bio, 'Freight coordinator')
        self.assertEqual(profile.phone, '+1 555 0100')

    def test_api_profile_save_email_change(self):
        response = self.client.post(reverse('users:api_profile_save'), {
            'email': 'alice.new@example.com',
            'phone': '',
            'bio': '',
        })
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'alice.new@example.com')

    def test_api_profile_email_taken(self):
        CustomUser.objects.create_user(email='bob@example.com', password='pass1234')
        response = self.client.post(reverse('users:api_profile_save'), {
            'email': 'bob@example.com',
            'phone': '',
            'bio': '',
        })
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data['ok'])

    def test_address_crud(self):
        create_url = reverse('users:api_address_create')
        response = self.client.post(create_url, {
            'label': 'Office', 'street': '1 Market St', 'city': 'Nairobi',
            'postal_code': '00100', 'country': 'Kenya', 'is_primary': True,
        })
        self.assertEqual(response.status_code, 200)
        addr = Address.objects.get(user=self.user)
        self.assertEqual(addr.label, 'Office')
        self.assertEqual(addr.city, 'Nairobi')
        self.assertTrue(addr.is_primary)

        update_url = reverse('users:api_address_update', args=[addr.pk])
        response = self.client.post(update_url, {
            'label': 'Head Office', 'street': '1 Market St', 'city': 'Nairobi',
            'postal_code': '00100', 'country': 'Kenya', 'is_primary': False,
        })
        self.assertEqual(response.status_code, 200)
        addr.refresh_from_db()
        self.assertEqual(addr.label, 'Head Office')
        self.assertFalse(addr.is_primary)

        profile = self.client.get(reverse('users:api_profile')).json()
        self.assertEqual(len(profile['addresses']), 1)

        delete_url = reverse('users:api_address_delete', args=[addr.pk])
        response = self.client.post(delete_url, {})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Address.objects.filter(pk=addr.pk).exists())

    def test_address_primary_exclusive(self):
        url = reverse('users:api_address_create')
        self.client.post(url, {'label': 'Home', 'street': 'A', 'is_primary': True})
        self.client.post(url, {'label': 'Office', 'street': 'B', 'is_primary': True})
        self.assertEqual(Address.objects.filter(user=self.user, is_primary=True).count(), 1)