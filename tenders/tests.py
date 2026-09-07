from django.test import TestCase
from django.urls import reverse

from tenders.models import Town
from users.models import CustomUser


class TownApiTest(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user(email='routes@example.com', password='pass1234')
        self.client.login(email='routes@example.com', password='pass1234')
        Town.objects.update_or_create(
            name='Nairobi', defaults={'country': 'Kenya', 'latitude': '-1.292100', 'longitude': '36.821900'},
        )
        Town.objects.update_or_create(
            name='Mombasa', defaults={'country': 'Kenya', 'latitude': '-4.043500', 'longitude': '39.668200'},
        )

    def test_api_towns(self):
        response = self.client.get(reverse('tenders:api_towns'))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['ok'])
        towns = {t['name']: t for t in data['towns']}
        self.assertIn('Nairobi', towns)
        self.assertAlmostEqual(towns['Nairobi']['lat'], -1.2921)
        self.assertAlmostEqual(towns['Nairobi']['lng'], 36.8219)

    def test_api_town_route(self):
        url = reverse('tenders:api_town_route')
        response = self.client.get(url, {'from': 'Nairobi', 'to': 'Mombasa'})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['origin']['name'], 'Nairobi')
        self.assertEqual(data['destination']['name'], 'Mombasa')

    def test_api_town_route_missing_params(self):
        response = self.client.get(reverse('tenders:api_town_route'), {'from': 'Nairobi'})
        self.assertEqual(response.status_code, 400)

    def test_api_town_route_unknown_town(self):
        response = self.client.get(reverse('tenders:api_town_route'), {'from': 'Nairobi', 'to': 'Atlantis'})
        self.assertEqual(response.status_code, 404)

    def test_route_map_page(self):
        response = self.client.get(reverse('tenders:route_map'), {'from': 'Nairobi', 'to': 'Mombasa'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Route Map')
        self.assertContains(response, '"/api/towns/"')
        self.assertContains(response, 'const loadingTown = "Nairobi";')