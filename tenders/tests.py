from datetime import timedelta
import json
from unittest.mock import Mock, patch

import requests

from django.contrib.gis.geos import Point
from django.core.mail.backends.console import EmailBackend as ConsoleBackend
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from DjangoProject.mail_backend import ApiSettingEmailBackend
from tenders import views as tenders_views
from tenders import selcom
from tenders.models import ApiSetting, Invoice, OdooCompany, Order, OrderLine, Tender, Town
from companies.models import Company
from users.models import CustomUser


class TownApiTest(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user(email='routes@example.com', password='pass1234')
        self.client.login(email='routes@example.com', password='pass1234')
        Town.objects.update_or_create(
            name='Nairobi', defaults={'country': 'Kenya', 'point': Point(36.8219, -1.2921, srid=4326)},
        )
        Town.objects.update_or_create(
            name='Mombasa', defaults={'country': 'Kenya', 'point': Point(39.6682, -4.0435, srid=4326)},
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

    def test_new_tender_page_has_route_map(self):
        response = self.client.get(reverse('tenders:create'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'tender-route-map')
        self.assertContains(response, 'data-towns-url')


class OrderListPerformanceTest(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user(email='orders@example.com', password='pass1234')
        self.client.login(email='orders@example.com', password='pass1234')
        self.tender = Tender.objects.create(
            user=self.user, route_loading='Nairobi', route_delivery='Mombasa',
            customer='Acme', cargo_type=Tender.CargoType.DRY_VAN,
            truck_type=Tender.TruckType.TRUCK, weight=20.0, number_of_trucks=1,
            distance_km=480, cargo_date=timezone.localdate(), status=Tender.Status.SUCCESS,
            cargo_reference='REF-X',
        )

    def _make_orders(self, n, start=0):
        for i in range(n):
            order = Order.objects.create(
                order_id=1000 + start + i, order_name=f'ORD-{start + i}', user=self.user, tender=self.tender,
            )
            OrderLine.objects.create(
                order=order, line_id=1, product_name='Sand', quantity=1, price_unit=100,
                commission=0, price_subtotal=100, price_total=100,
                awarded=(i % 2 == 0),
            )

    def test_order_list_query_count_does_not_grow_with_orders(self):
        self._make_orders(5)
        with CaptureQueriesContext(connection) as first:
            self.client.get(reverse('tenders:api_order_list'))
        self._make_orders(15, start=100)
        with CaptureQueriesContext(connection) as second:
            self.client.get(reverse('tenders:api_order_list'))
        self.assertEqual(len(first.captured_queries), len(second.captured_queries))

    def test_order_list_returns_annotated_aggregates(self):
        self._make_orders(4)
        response = self.client.get(reverse('tenders:api_order_list'))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['ok'])
        orders = [o for group in data['groups'] for o in group['orders']]
        self.assertEqual(len(orders), 4)
        awarded = [o for o in orders if o['order_id'] % 2 == 0]
        for o in awarded:
            self.assertEqual(o['total_lines_count'], 1)
            self.assertEqual(o['awarded_lines_count'], 1)
            self.assertEqual(o['awarded_amount'], '100.00')
        for o in [o for o in orders if o['order_id'] % 2 != 0]:
            self.assertEqual(o['awarded_lines_count'], 0)
            self.assertEqual(o['awarded_amount'], '0')

    def test_order_list_groups_by_tender_reference(self):
        second = Tender.objects.create(
            user=self.user, route_loading='Nairobi', route_delivery='Mombasa',
            customer='Beta', cargo_type=Tender.CargoType.DRY_VAN,
            truck_type=Tender.TruckType.TRUCK, weight=20.0, number_of_trucks=1,
            distance_km=480, cargo_date=timezone.localdate(),
            reference='HYPAX-000991', cargo_reference='LEGACY-CARGO-REF',
        )
        Order.objects.create(order_id=6001, order_name='ORD-6001', user=self.user, tender=self.tender)
        Order.objects.create(order_id=6002, order_name='ORD-6002', user=self.user, tender=second)
        response = self.client.get(reverse('tenders:api_order_list'))
        groups = response.json()['groups']
        keys = {g['grouper'] for g in groups}
        self.assertEqual(keys, {'REF-X', 'HYPAX-000991'})
        by_key = {g['grouper']: g for g in groups}
        self.assertEqual(by_key['REF-X']['label'], 'REF-X')
        self.assertEqual(len(by_key['HYPAX-000991']['orders']), 1)
        self.assertNotIn('LEGACY-CARGO-REF', by_key['HYPAX-000991']['grouper'])

    def test_order_list_includes_unlinked_orders(self):
        Order.objects.create(order_id=7777, order_name='ORD-7777', user=None, tender=None)
        response = self.client.get(reverse('tenders:api_order_list'))
        order_ids = [o['order_id'] for group in response.json()['groups'] for o in group['orders']]
        self.assertIn(7777, order_ids)

    def test_order_list_paginated_15_per_page(self):
        self._make_orders(20)
        response = self.client.get(reverse('tenders:api_order_list'))
        data = response.json()
        self.assertEqual(data['page'], 1)
        self.assertEqual(data['pages'], 2)
        self.assertEqual(data['per_page'], 15)
        self.assertTrue(data['has_next'])
        self.assertFalse(data['has_prev'])
        self.assertEqual(sum(len(g['orders']) for g in data['groups']), 15)
        response = self.client.get(reverse('tenders:api_order_list'), {'page': 2})
        data = response.json()
        self.assertEqual(data['page'], 2)
        self.assertFalse(data['has_next'])
        self.assertEqual(sum(len(g['orders']) for g in data['groups']), 5)

    def test_order_list_page_out_of_range_clamps(self):
        response = self.client.get(reverse('tenders:api_order_list'), {'page': 99})
        data = response.json()
        self.assertEqual(data['page'], 1)

    def test_admin_sees_orders_from_other_companies(self):
        admin = CustomUser.objects.create_user(
            email='order-admin@example.com', password='pass1234',
            role=CustomUser.Role.ADMINISTRATOR,
        )
        other = CustomUser.objects.create_user(email='other-company@example.com', password='pass1234')
        Order.objects.create(
            order_id=5001, order_name='ORD-5001', user=other, tender=self.tender,
            state='confirmed',
        )
        self.client.logout()
        self.client.force_login(admin)
        response = self.client.get(reverse('tenders:api_order_list'))
        ids = {o['order_id'] for g in response.json()['groups'] for o in g['orders']}
        self.assertIn(5001, ids)
        self.client.logout()
        self.client.force_login(other)
        response = self.client.get(reverse('tenders:api_order_list'))
        ids = {o['order_id'] for g in response.json()['groups'] for o in g['orders']}
        self.assertIn(5001, ids)


class NotifyTest(TestCase):
    def test_notify_broadcasts_unlinked_order_to_all(self):
        sent = []

        class FakeLayer:
            async def group_send(self, group, message):
                sent.append((group, message))

        with patch('tenders.views.get_channel_layer', return_value=FakeLayer()):
            order = Order.objects.create(order_id=5001, order_name='ORD-5001')
            tenders_views.notify_order_update(order)
        self.assertIn(('orders_all', {'type': 'order.update', 'data': {'action': 'refresh'}}), sent)
        self.assertTrue(any(g == f'order_{order.pk}' for g, _ in sent))

    def test_notify_sends_linked_order_to_user_group(self):
        user = CustomUser.objects.create_user(email='owner@example.com', password='pass1234')
        sent = []

        class FakeLayer:
            async def group_send(self, group, message):
                sent.append(group)

        with patch('tenders.views.get_channel_layer', return_value=FakeLayer()):
            order = Order.objects.create(order_id=5002, order_name='ORD-5002', user=user)
            tenders_views.notify_order_update(order)
        self.assertIn(f'orders_user_{user.pk}', sent)
        self.assertNotIn('orders_all', sent)

    def test_notify_always_broadcasts_to_admins(self):
        user = CustomUser.objects.create_user(email='owner2@example.com', password='pass1234')
        sent = []

        class FakeLayer:
            async def group_send(self, group, message):
                sent.append(group)

        with patch('tenders.views.get_channel_layer', return_value=FakeLayer()):
            order = Order.objects.create(order_id=5101, order_name='ORD-5101', user=user)
            tenders_views.notify_order_update(order)
            unassigned = Order.objects.create(order_id=5102, order_name='ORD-5102')
            tenders_views.notify_order_update(unassigned)
        self.assertGreaterEqual(sent.count('orders_admins'), 2)


class TrackerTest(TestCase):
    def setUp(self):
        self.osrm_patcher = patch('tenders.views._osrm_fetch', side_effect=OSError('no network'))
        self.addCleanup(self.osrm_patcher.stop)
        self.osrm_patcher.start()
        tenders_views._route_cache.clear()
        self.user = CustomUser.objects.create_user(email='tracker@example.com', password='pass1234')
        self.client.login(email='tracker@example.com', password='pass1234')
        Town.objects.update_or_create(
            name='Nairobi', defaults={'country': 'Kenya', 'point': Point(36.8219, -1.2921, srid=4326)},
        )
        Town.objects.update_or_create(
            name='Mombasa', defaults={'country': 'Kenya', 'point': Point(39.6682, -4.0435, srid=4326)},
        )
        self.tender = Tender.objects.create(
            user=self.user,
            route_loading='Nairobi',
            route_delivery='Mombasa',
            customer='Acme',
            cargo_type=Tender.CargoType.DRY_VAN,
            truck_type=Tender.TruckType.TRUCK,
            weight=20.0,
            number_of_trucks=2,
            distance_km=480,
            cargo_date=timezone.localdate(),
            status=Tender.Status.SUCCESS,
        )

    def _create_awarded_order(self, order_id, order_name, tender=None, awarded=True):
        order = Order.objects.create(
            order_id=order_id, order_name=order_name, customer='Acme',
            user=self.user, tender=tender or self.tender,
        )
        OrderLine.objects.create(
            order=order, line_id=1, product_name='Sand', quantity=1, price_unit=100,
            commission=0, price_subtotal=100, price_total=100, awarded=awarded,
        )
        return order

    def test_tracker_page_renders(self):
        response = self.client.get(reverse('tenders:tracker'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'tracker-map')
        self.assertContains(response, 'data-url')

    def test_route_map_locates_single_truck(self):
        order = self._create_awarded_order(9015, 'ORD-9015')
        response = self.client.get(reverse('tenders:route_map'), {
            'from': 'Nairobi',
            'to': 'Mombasa',
            'source': 'invoices',
            'truck': '2',
            'cargo_ref': order.cargo_reference,
            'tender_id': order.tender_id,
        })
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('Locating', html)
        self.assertIn('T2', html)
        self.assertIn('const truckLat = "-', html)

    def test_api_tracker_returns_awarded_orders(self):
        self._create_awarded_order(9000, 'ORD-9000')
        self._create_awarded_order(9001, 'ORD-9001')
        response = self.client.get(reverse('tenders:api_tracker'))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['ok'])
        self.assertEqual(len(data['groups']), 1)
        group = data['groups'][0]
        self.assertEqual([o['order_name'] for o in group['orders']], ['ORD-9001', 'ORD-9000'])
        self.assertEqual(group['origin']['name'], 'Nairobi')
        self.assertEqual(group['destination']['name'], 'Mombasa')
        self.assertAlmostEqual(group['distance_km'], 480, delta=60)
        self.assertEqual(group['tender_ref'], self.tender.tender_reference())
        self.assertGreaterEqual(len(group['trucks']), 1)
        for truck in group['trucks']:
            self.assertIn(truck['status'], ('En route', 'Delivered'))
            self.assertIn('lat', truck)
            self.assertIn('lng', truck)

    def test_api_tracker_skips_orders_without_awarded_lines(self):
        self._create_awarded_order(9002, 'ORD-9002')
        self._create_awarded_order(9003, 'ORD-9003', awarded=False)
        Order.objects.create(order_id=9004, order_name='ORD-9004', user=self.user)
        response = self.client.get(reverse('tenders:api_tracker'))
        self.assertEqual(response.status_code, 200)
        groups = response.json()['groups']
        self.assertEqual(len(groups), 1)
        names = [o['order_name'] for o in groups[0]['orders']]
        self.assertEqual(names, ['ORD-9002'])

    def test_api_tracker_only_shows_orders_of_own_tenders(self):
        other = CustomUser.objects.create_user(email='other@example.com', password='pass1234')
        other_tender = Tender.objects.create(
            user=other, route_loading='Nairobi', route_delivery='Mombasa',
            customer='Other', cargo_type=Tender.CargoType.DRY_VAN,
            truck_type=Tender.TruckType.TRUCK, weight=5.0, number_of_trucks=1,
            distance_km=480, cargo_date=timezone.localdate(), status=Tender.Status.SUCCESS,
        )
        self._create_awarded_order(9010, 'ORD-9010')
        order = Order.objects.create(
            order_id=9011, order_name='ORD-9011', customer='Other',
            user=other, tender=other_tender,
        )
        OrderLine.objects.create(
            order=order, line_id=1, product_name='Sand', quantity=1, price_unit=100,
            commission=0, price_subtotal=100, price_total=100, awarded=True,
        )
        response = self.client.get(reverse('tenders:api_tracker'))
        self.assertEqual(response.status_code, 200)
        groups = response.json()['groups']
        names = [o['order_name'] for g in groups for o in g['orders']]
        self.assertEqual(names, ['ORD-9010'])

    def test_api_tracker_admin_sees_all_companies_orders(self):
        admin = CustomUser.objects.create_user(
            email='tracker-admin@example.com', password='pass1234',
            role=CustomUser.Role.ADMINISTRATOR,
        )
        other = CustomUser.objects.create_user(email='tracker-other@example.com', password='pass1234')
        other_tender = Tender.objects.create(
            user=other, route_loading='Nairobi', route_delivery='Mombasa',
            customer='Other', cargo_type=Tender.CargoType.DRY_VAN,
            truck_type=Tender.TruckType.TRUCK, weight=5.0, number_of_trucks=1,
            distance_km=480, cargo_date=timezone.localdate(), status=Tender.Status.SUCCESS,
        )
        self._create_awarded_order(9012, 'ORD-9012')
        order = Order.objects.create(
            order_id=9013, order_name='ORD-9013', customer='Other',
            user=other, tender=other_tender,
        )
        OrderLine.objects.create(
            order=order, line_id=1, product_name='Sand', quantity=1, price_unit=100,
            commission=0, price_subtotal=100, price_total=100, awarded=True,
        )
        self.client.logout()
        self.client.force_login(admin)
        response = self.client.get(reverse('tenders:api_tracker'))
        self.assertEqual(response.status_code, 200)
        groups = response.json()['groups']
        names = [o['order_name'] for g in groups for o in g['orders']]
        self.assertIn('ORD-9013', names)
        self.assertIn('ORD-9012', names)

    def test_api_tracker_uses_trucks_from_order_tender(self):
        pending = Tender.objects.create(
            user=self.user,
            route_loading='Nairobi',
            route_delivery='Mombasa',
            customer='Beta',
            cargo_type=Tender.CargoType.BULK,
            truck_type=Tender.TruckType.TRAILER,
            weight=10.0,
            number_of_trucks=3,
            distance_km=480,
            cargo_date=timezone.localdate(),
            status=Tender.Status.PENDING,
        )
        self._create_awarded_order(9005, 'ORD-9005', tender=pending)
        self._create_awarded_order(9006, 'ORD-9006')
        response = self.client.get(reverse('tenders:api_tracker'))
        self.assertEqual(response.status_code, 200)
        by_order = {}
        for group in response.json()['groups']:
            for o in group['orders']:
                by_order[o['order_name']] = group
        self.assertEqual(len(by_order['ORD-9005']['trucks']), 3)
        self.assertEqual(len(by_order['ORD-9006']['trucks']), 2)

    def test_api_tracker_returns_route_points(self):
        self._create_awarded_order(9007, 'ORD-9007')
        response = self.client.get(reverse('tenders:api_tracker'))
        group = response.json()['groups'][0]
        self.assertEqual(group['route'][0], [group['origin']['lat'], group['origin']['lng']])
        self.assertEqual(group['route'][-1], [group['destination']['lat'], group['destination']['lng']])
        self.assertGreaterEqual(group['distance_km'], 0)

    def test_get_route_uses_osrm_when_available(self):
        origin = Town.objects.get(name='Nairobi')
        dest = Town.objects.get(name='Mombasa')
        fake = ([
            [origin.lat, origin.lng], [0.0, 37.0], [-2.5, 39.2], [dest.lat, dest.lng],
        ], 523000.0)
        with patch('tenders.views._osrm_fetch', return_value=fake):
            points, distance = tenders_views.get_route(origin, dest)
        self.assertEqual(len(points), 4)
        self.assertEqual(distance, 523000.0)

    def test_position_at_interpolates_along_route(self):
        points = [[-1.0, 36.0], [-2.0, 37.0], [-3.0, 38.0]]
        distances = tenders_views._route_arrays(points)
        for p in points:
            self.assertGreaterEqual(p[0], -3.0)
            self.assertLessEqual(p[0], -1.0)
        mid = tenders_views._position_at(points, distances, 0.5)
        self.assertGreater(mid[1], 36.0)
        self.assertLess(mid[1], 38.0)
        start = tenders_views._position_at(points, distances, 0.0)
        self.assertEqual(start, points[0])
        end = tenders_views._position_at(points, distances, 1.0)
        self.assertEqual(end, points[-1])


class TenderListPaginationTest(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user(email='tl@example.com', password='pass1234')
        self.client.login(email='tl@example.com', password='pass1234')
        for i in range(17):
            Tender.objects.create(
                user=self.user, route_loading='Nairobi', route_delivery='Mombasa',
                customer=f'C{i}', cargo_type=Tender.CargoType.DRY_VAN,
                truck_type=Tender.TruckType.TRUCK, weight=10.0, number_of_trucks=1,
                distance_km=480, cargo_date=timezone.localdate(),
            )

    def test_api_tender_list_paginates_15_per_page(self):
        response = self.client.get(reverse('tenders:api_tender_list'))
        data = response.json()
        self.assertEqual(data['page'], 1)
        self.assertEqual(data['pages'], 2)
        self.assertEqual(data['per_page'], 15)
        self.assertEqual(data['total'], 17)
        self.assertTrue(data['has_next'])
        self.assertEqual(len(data['tenders']), 15)
        response = self.client.get(reverse('tenders:api_tender_list'), {'page': 2})
        data = response.json()
        self.assertEqual(len(data['tenders']), 2)
        self.assertFalse(data['has_next'])
        self.assertTrue(data['has_prev'])


class SharedSettingTest(TestCase):
    """The shared/legacy Odoo API settings page and JSON endpoints were removed.

    The ApiSetting singleton still exists for Selcom/email/media, so these tests
    assert the removed endpoints 404 and the platform-wide paths remain on the model.
    """

    def setUp(self):
        from tenders.models import ApiSetting
        self.admin = CustomUser.objects.create_user(
            email='admin@example.com', password='pass1234', role=CustomUser.Role.ADMINISTRATOR,
        )
        self.user = CustomUser.objects.create_user(email='user@example.com', password='pass1234')
        self.setting = ApiSetting.objects.create(base_url='https://odo.example.com/')

    def test_shared_settings_page_removed(self):
        self.client.login(email='admin@example.com', password='pass1234')
        self.assertEqual(self.client.get('/settings/').status_code, 404)
        with self.assertRaises(NoReverseMatch):
            reverse('tenders:api_settings')

    def test_shared_settings_json_removed(self):
        self.client.login(email='admin@example.com', password='pass1234')
        self.assertEqual(self.client.get('/api/settings/').status_code, 404)
        with self.assertRaises(NoReverseMatch):
            reverse('tenders:api_settings_json')

    def test_endpoint_paths_default_when_blank(self):
        self.assertEqual(self.setting.endpoint_url(), 'https://odo.example.com/api/v1/tenders')
        self.assertEqual(self.setting.order_confirmation_url(), 'https://odo.example.com/api/v1/order-confirmation')
        self.assertEqual(self.setting.partial_order_confirmation_url(), 'https://odo.example.com/api/v1/partial-order-confirmation')
        self.assertEqual(self.setting.order_invoice_url(), 'https://odo.example.com/api/v1/order-invoice')

    def test_endpoint_paths_use_custom_values(self):
        self.setting.base_url = 'https://odo.example.com/'
        self.setting.tenders_path = '/tenders/v2'
        self.setting.order_confirmation_path = 'confirm/v2'
        self.setting.partial_order_confirmation_path = '/partial/v2'
        self.setting.order_invoice_path = 'invoice/v2'
        self.setting.save()
        self.assertEqual(self.setting.endpoint_url(), 'https://odo.example.com/tenders/v2')
        self.assertEqual(self.setting.order_confirmation_url(), 'https://odo.example.com/confirm/v2')
        self.assertEqual(self.setting.partial_order_confirmation_url(), 'https://odo.example.com/partial/v2')
        self.assertEqual(self.setting.order_invoice_url(), 'https://odo.example.com/invoice/v2')


class InvoicesPageTest(TestCase):
    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            email='invoice-admin@example.com', password='pass1234',
            role=CustomUser.Role.ADMINISTRATOR,
        )
        self.user_a = CustomUser.objects.create_user(email='invoice-a@example.com', password='pass1234')
        self.user_b = CustomUser.objects.create_user(email='invoice-b@example.com', password='pass1234')
        self.company = OdooCompany.objects.create(
            name='Invoices Transporter', base_url='https://odo.example.com/', auth_type='bearer',
        )

    def _tender(self, user, ref):
        return Tender.objects.create(
            user=user, route_loading='Nairobi', route_delivery='Mombasa',
            customer=f'C-{ref}', cargo_type=Tender.CargoType.DRY_VAN,
            truck_type=Tender.TruckType.TRUCK, weight=10.0, number_of_trucks=1,
            distance_km=480, cargo_date=timezone.localdate(), cargo_reference=ref,
        )

    def _order(self, user, tender, order_id, ref, company=None):
        order = Order.objects.create(
            order_id=order_id, order_name=f'ORD-{order_id}', user=user, tender=tender,
            company_id=1, company_name='Alpha Haulage', cargo_reference=ref, state='confirmed',
            amount_total=0, currency='USD', odoo_company=company if company is not None else self.company,
        )
        OrderLine.objects.create(
            order=order, line_id=order_id, product_name='Sand', quantity=1,
            price_unit=100, commission=0, price_subtotal=100, price_total=100, awarded=True,
        )
        from tenders.views import get_or_create_invoice
        return get_or_create_invoice(order)

    def test_admin_sees_all_invoices(self):
        inv_a = self._order(self.user_a, self._tender(self.user_a, 'REF-A'), 7001, 'REF-A')
        inv_b = self._order(self.user_b, self._tender(self.user_b, 'REF-B'), 7002, 'REF-B')
        for inv in (inv_a, inv_b):
            inv.status = 'paid'
            inv.save(update_fields=('status',))
        self.client.login(email='invoice-admin@example.com', password='pass1234')
        response = self.client.get(reverse('tenders:api_invoices'))
        data = response.json()
        self.assertTrue(data['ok'])
        ids = {i['id'] for i in data['invoices']}
        self.assertEqual(ids, {inv_a.pk, inv_b.pk})

    def test_pending_invoices_now_returned_with_selcom_flag(self):
        inv_a = self._order(self.user_a, self._tender(self.user_a, 'REF-A'), 7010, 'REF-A')
        inv_b = self._order(self.user_a, self._tender(self.user_a, 'REF-B'), 7011, 'REF-B')
        inv_b.status = 'paid'
        inv_b.save(update_fields=('status',))
        self.client.login(email='invoice-admin@example.com', password='pass1234')
        response = self.client.get(reverse('tenders:api_invoices'))
        data = response.json()
        ids = [i['id'] for i in data['invoices']]
        self.assertIn(inv_b.pk, ids)
        self.assertIn(inv_a.pk, ids)
        self.assertIn('selcom_enabled', data)

    def test_user_sees_only_own_invoices(self):
        inv_a = self._order(self.user_a, self._tender(self.user_a, 'REF-A'), 7003, 'REF-A')
        inv_b = self._order(self.user_b, self._tender(self.user_b, 'REF-B'), 7004, 'REF-B')
        for inv in (inv_a, inv_b):
            inv.status = 'paid'
            inv.save(update_fields=('status',))
        self.client.login(email='invoice-a@example.com', password='pass1234')
        response = self.client.get(reverse('tenders:api_invoices'))
        data = response.json()
        self.assertTrue(data['ok'])
        ids = [i['id'] for i in data['invoices']]
        self.assertEqual(ids, [inv_a.pk])

    def test_order_list_includes_payment_status(self):
        invite = self._order(self.user_a, self._tender(self.user_a, 'REF-A'), 7012, 'REF-A')
        self.client.login(email='invoice-a@example.com', password='pass1234')
        response = self.client.get(reverse('tenders:api_order_list'))
        orders = [o for group in response.json()['groups'] for o in group['orders']]
        order = next(o for o in orders if o['order_id'] == 7012)
        self.assertEqual(order['invoice_id'], invite.pk)
        self.assertEqual(order['payment_status'], 'pending')

    def test_scoped_user_can_mark_paid(self):
        invite = self._order(self.user_a, self._tender(self.user_a, 'REF-A'), 7005, 'REF-A')
        self.client.login(email='invoice-a@example.com', password='pass1234')
        with patch('tenders.views.submit_confirmation',
                   return_value=(200, ('{"status":"success","data":[{"id":41,"name":"INV/2026/00012",'
                                       '"state":"posted","amount_total":1150.0}]}'), True)):
            response = self.client.post(reverse('tenders:api_invoice_paid', args=[invite.pk]), {})
        data = response.json()
        self.assertTrue(data['ok'])
        invite.refresh_from_db()
        self.assertEqual(invite.status, 'paid')
        self.assertEqual(invite.number, 'INV/2026/00012')

    def test_other_user_cannot_mark_paid(self):
        invite = self._order(self.user_a, self._tender(self.user_a, 'REF-A'), 7014, 'REF-A')
        self.client.login(email='invoice-b@example.com', password='pass1234')
        response = self.client.post(reverse('tenders:api_invoice_paid', args=[invite.pk]), {})
        self.assertEqual(response.status_code, 404)

    def test_admin_can_mark_paid(self):
        Company.objects.create(
            user=self.user_a, name='Acme Logistics', tin='TIN-123', vat='VAT-9', country='TZ',
        )
        invite = self._order(self.user_a, self._tender(self.user_a, 'REF-A'), 7006, 'REF-A')
        self.client.login(email='invoice-admin@example.com', password='pass1234')
        with patch('tenders.views.submit_confirmation',
                   return_value=(200, ('{"status":"success","data":[{"id":41,"name":"INV/2026/00045",'
                                       '"state":"posted","amount_total":1150.0}]}'), True)) as m:
            response = self.client.post(reverse('tenders:api_invoice_paid', args=[invite.pk]), {})
        data = response.json()
        self.assertTrue(data['ok'])
        url, payload = m.call_args[0][1], m.call_args[0][2]
        self.assertEqual(url, 'https://odo.example.com/api/v1/order-invoice')
        self.assertEqual(payload, {
            'order_id': 7006,
            'cargo_name': 'REF-A',
            'tender_reference': 'REF-A',
            'customer_name': '',
            'tax_id': 'TIN-123',
            'country': 'TZ',
        })
        invite.refresh_from_db()
        self.assertEqual(invite.status, 'paid')
        self.assertEqual(invite.number, 'INV/2026/00045')

    def test_admin_mark_paid_dict_data_format(self):
        invite = self._order(self.user_a, self._tender(self.user_a, 'REF-A'), 7016, 'REF-A')
        self.client.login(email='invoice-admin@example.com', password='pass1234')
        with patch('tenders.views.submit_confirmation',
                   return_value=(200, '{"status":"success","data":{"name":"EXT-INV-7016"}}', True)):
            response = self.client.post(reverse('tenders:api_invoice_paid', args=[invite.pk]), {})
        self.assertTrue(response.json()['ok'])
        invite.refresh_from_db()
        self.assertEqual(invite.number, 'EXT-INV-7016')

    def test_admin_mark_paid_without_external_name_keeps_local_number(self):
        invite = self._order(self.user_a, self._tender(self.user_a, 'REF-A'), 7015, 'REF-A')
        self.client.login(email='invoice-admin@example.com', password='pass1234')
        with patch('tenders.views.submit_confirmation', return_value=(200, '{"status":"success"}', True)):
            response = self.client.post(reverse('tenders:api_invoice_paid', args=[invite.pk]), {})
        self.assertTrue(response.json()['ok'])
        invite.refresh_from_db()
        self.assertEqual(invite.status, 'paid')
        self.assertEqual(invite.number, 'INV-7015')

    def test_admin_mark_paid_requires_external_success(self):
        invite = self._order(self.user_a, self._tender(self.user_a, 'REF-A'), 7008, 'REF-A')
        self.client.login(email='invoice-admin@example.com', password='pass1234')
        with patch('tenders.views.submit_confirmation', return_value=(500, 'boom', False)):
            response = self.client.post(reverse('tenders:api_invoice_paid', args=[invite.pk]), {})
        data = response.json()
        self.assertFalse(data['ok'])
        invite.refresh_from_db()
        self.assertEqual(invite.status, 'pending')

    def test_admin_mark_paid_without_company_rejected(self):
        from tenders.views import get_or_create_invoice
        tender = self._tender(self.user_a, 'REF-A')
        order = Order.objects.create(
            order_id=7009, order_name='ORD-7009', user=self.user_a, tender=tender,
            company_id=1, company_name='Alpha Haulage', cargo_reference='REF-A', state='confirmed',
            amount_total=0, currency='USD', odoo_company=None,
        )
        invite = get_or_create_invoice(order)
        self.client.login(email='invoice-admin@example.com', password='pass1234')
        response = self.client.post(reverse('tenders:api_invoice_paid', args=[invite.pk]), {})
        data = response.json()
        self.assertFalse(data['ok'])
        self.assertIn('no Odoo company', data['error'])
        invite.refresh_from_db()
        self.assertEqual(invite.status, 'pending')

    def test_order_detail_includes_route_fields(self):
        invite = self._order(self.user_a, self._tender(self.user_a, 'REF-A'), 7017, 'REF-A')
        self.client.login(email='invoice-a@example.com', password='pass1234')
        response = self.client.get(reverse('tenders:api_order_detail', args=[invite.order.pk]))
        order = response.json()['order']
        self.assertEqual(order['tender_loading'], 'Nairobi')
        self.assertEqual(order['tender_delivery'], 'Mombasa')
        self.assertEqual(order['tender_route'], 'Nairobi -> Mombasa')

    def test_invoice_dict_includes_route_and_cargo_fields(self):
        invite = self._order(self.user_a, self._tender(self.user_a, 'REF-A'), 7018, 'REF-A')
        invite.status = 'paid'
        invite.save(update_fields=('status',))
        self.client.login(email='invoice-admin@example.com', password='pass1234')
        response = self.client.get(reverse('tenders:api_invoices'))
        data = response.json()['invoices']
        inv_data = next(i for i in data if i['id'] == invite.pk)
        self.assertEqual(inv_data['tender_loading'], 'Nairobi')
        self.assertEqual(inv_data['tender_delivery'], 'Mombasa')
        self.assertTrue('Nairobi' in inv_data['route'] and 'Mombasa' in inv_data['route'])
        self.assertIn('cargo_id', inv_data)
        self.assertIn('order_id', inv_data)
        self.assertIn('customer', inv_data)
        self.assertIsNotNone(inv_data['tender_id'])
        self.assertEqual(len(inv_data['lines']), 1)
        self.assertEqual(inv_data['lines'][0]['product_name'], 'Sand')
        self.assertEqual(inv_data['lines'][0]['price_subtotal'], '100.00')
        self.assertEqual(inv_data['lines'][0]['tax'], '0.00')
        self.assertEqual(inv_data['lines'][0]['price_total'], '100.00')

    def test_invoices_page_renders(self):
        self.client.login(email='invoice-a@example.com', password='pass1234')
        response = self.client.get(reverse('tenders:invoices'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '/api/invoices/')

    def _awarded_order(self, order_id, ref, user=None):
        from tenders.models import Order, OrderLine
        tender = self._tender(user or self.user_a, f'REF-{ref}')
        order = Order.objects.create(
            order_id=order_id, order_name=f'ORD-{order_id}', user=user or self.user_a, tender=tender,
            company_id=1, company_name='Alpha Haulage', cargo_reference=f'REF-{ref}', state='confirmed',
            amount_total=0, currency='USD',
        )
        OrderLine.objects.create(
            order=order, line_id=order_id, product_name='Sand', quantity=1,
            price_unit=100, commission=0, price_subtotal=100, price_total=100, awarded=True,
        )
        return order

    def test_invoices_api_does_not_create_invoice_or_escrow(self):
        from tenders.models import EscrowAccount, Invoice
        order = self._awarded_order(7090, 'DEFER')
        self.client.login(email='invoice-a@example.com', password='pass1234')
        data = self.client.get(reverse('tenders:api_invoices')).json()
        row = next(r for r in data['invoices'] if r['order_id'] == 7090)
        self.assertIsNone(row['id'])
        self.assertEqual(row['order_pk'], order.pk)
        self.assertEqual(row['number'], 'INV-7090')
        self.assertEqual(row['status'], 'pending')
        self.assertEqual(row['amount_total'], '100.00')
        self.assertFalse(Invoice.objects.filter(order=order).exists())
        self.assertFalse(EscrowAccount.objects.filter(tender=order.tender).exists())

    def test_order_pay_creates_invoice_and_escrow_on_click(self):
        from tenders.models import EscrowAccount, Invoice
        order = self._awarded_order(7091, 'PAY')
        self.client.login(email='invoice-a@example.com', password='pass1234')
        response = self.client.post(reverse('tenders:api_order_pay', args=[order.pk]), {})
        self.assertFalse(response.json()['ok'])
        invoice = Invoice.objects.get(order=order)
        self.assertEqual(invoice.number, 'INV-7091')
        self.assertEqual(invoice.status, 'pending')
        escrow = EscrowAccount.objects.filter(tender=order.tender).first()
        self.assertIsNotNone(escrow)
        self.assertTrue(escrow.invoices.filter(pk=invoice.pk).exists())

    def test_order_checkout_creates_invoice_and_escrow_on_click(self):
        from tenders.models import EscrowAccount, Invoice
        order = self._awarded_order(7092, 'CHECKOUT')
        ApiSetting.objects.create(selcom_enabled=True, selcom_client_id='c', selcom_client_secret='s')
        self.client.login(email='invoice-a@example.com', password='pass1234')
        with patch('tenders.views.selcom.create_checkout_order', return_value={
            'order_token': 'tok-defer', 'pay_link': 'https://checkout/tok-defer',
        }):
            response = self.client.post(reverse('tenders:api_order_checkout', args=[order.pk]), {})
        self.assertTrue(response.json()['ok'])
        invoice = Invoice.objects.get(order=order)
        self.assertEqual(invoice.selcom_order_token, 'tok-defer')
        escrow = EscrowAccount.objects.filter(tender=order.tender).first()
        self.assertIsNotNone(escrow)
        self.assertTrue(escrow.invoices.filter(pk=invoice.pk).exists())


class DashboardRoleTest(TestCase):
    def test_agent_dashboard_hides_send_tender_buttons(self):
        agent = CustomUser.objects.create_user(
            email='agent.dash@example.com', password='pass1234', role=CustomUser.Role.AGENT,
        )
        self.client.login(email='agent.dash@example.com', password='pass1234')
        response = self.client.get(reverse('tenders:dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, '>+ Send tender')
        self.assertNotContains(response, 'data-can-tender')

    def test_user_dashboard_shows_send_tender_button(self):
        user = CustomUser.objects.create_user(email='user.dash@example.com', password='pass1234')
        self.client.login(email='user.dash@example.com', password='pass1234')
        response = self.client.get(reverse('tenders:dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '>+ Send tender')
        self.assertContains(response, 'data-can-tender')

    def test_dashboard_welcomes_user_by_full_name(self):
        CustomUser.objects.create_user(
            email='dash.name@example.com', password='pass1234',
            first_name='Alice', last_name='Mangu',
        )
        self.client.login(email='dash.name@example.com', password='pass1234')
        response = self.client.get(reverse('tenders:dashboard'))
        self.assertContains(response, 'Welcome back, Alice Mangu')
        self.assertNotContains(response, 'Welcome back, dash.name@example.com')


class AgentTenderAccessTest(TestCase):
    def setUp(self):
        self.agent = CustomUser.objects.create_user(
            email='agent.tender@example.com', password='pass1234', role=CustomUser.Role.AGENT,
        )
        self.user = CustomUser.objects.create_user(email='user.tender@example.com', password='pass1234')
        self.client.login(email='agent.tender@example.com', password='pass1234')

    def test_agent_cannot_access_tenders_list(self):
        response = self.client.get(reverse('tenders:list'))
        self.assertRedirects(response, reverse('users:agent_awarded'))

    def test_agent_cannot_access_new_tender_page(self):
        response = self.client.get(reverse('tenders:create'))
        self.assertEqual(response.status_code, 403)

    def test_regular_user_can_access_tenders_list(self):
        self.client.login(email='user.tender@example.com', password='pass1234')
        response = self.client.get(reverse('tenders:list'))
        self.assertEqual(response.status_code, 200)

    def test_regular_user_can_access_new_tender_page(self):
        self.client.login(email='user.tender@example.com', password='pass1234')
        response = self.client.get(reverse('tenders:create'))
        self.assertEqual(response.status_code, 200)


class AdminTenderVisibilityTest(TestCase):
    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            email='admin.all@example.com', password='pass1234', role=CustomUser.Role.ADMINISTRATOR,
        )
        self.user = CustomUser.objects.create_user(email='other.owner@example.com', password='pass1234')
        self.tender = Tender.objects.create(
            user=self.user, route_loading='Nairobi', route_delivery='Mombasa',
            customer='OtherC', cargo_type=Tender.CargoType.DRY_VAN,
            truck_type=Tender.TruckType.TRUCK, weight=10.0, number_of_trucks=1,
            distance_km=480, cargo_date=timezone.localdate(),
        )

    def test_admin_api_tender_list_shows_tenders_from_other_users(self):
        self.client.login(email='admin.all@example.com', password='pass1234')
        data = self.client.get(reverse('tenders:api_tender_list')).json()
        row = next(t for t in data['tenders'] if t['id'] == self.tender.id)
        self.assertIn('Nairobi -> Mombasa', row['route'])

    def test_regular_user_api_tender_list_hides_other_users_tenders(self):
        CustomUser.objects.create_user(email='third.owner@example.com', password='pass1234')
        self.client.login(email='third.owner@example.com', password='pass1234')
        data = self.client.get(reverse('tenders:api_tender_list')).json()
        ids = [t['id'] for t in data['tenders']]
        self.assertNotIn(self.tender.id, ids)

    def test_admin_can_open_tenders_page(self):
        self.client.login(email='admin.all@example.com', password='pass1234')
        response = self.client.get(reverse('tenders:list'))
        self.assertEqual(response.status_code, 200)


def _order_for(user, ref, order_id):
    tender = Tender.objects.create(
        user=user, route_loading='Nairobi', route_delivery='Mombasa',
        customer=f'C-{ref}', cargo_type=Tender.CargoType.DRY_VAN,
        truck_type=Tender.TruckType.TRUCK, weight=10.0, number_of_trucks=1,
        distance_km=480, cargo_date=timezone.localdate(), cargo_reference=ref,
    )
    order = Order.objects.create(
        order_id=order_id, order_name=f'ORD-{order_id}', user=user, tender=tender,
        company_id=1, company_name='Alpha Haulage', cargo_reference=ref, state='confirmed',
        amount_total=0, currency='TZS',
    )
    OrderLine.objects.create(
        order=order, line_id=order_id, product_name='Sand', quantity=1,
        price_unit=100, commission=0, price_subtotal=100, price_total=100, awarded=True,
    )
    from tenders.views import get_or_create_invoice
    return get_or_create_invoice(order)


class SelcomPaymentTest(TestCase):
    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            email='selcom-admin@example.com', password='pass1234',
            role=CustomUser.Role.ADMINISTRATOR,
        )
        self.owner = CustomUser.objects.create_user(email='selcom-owner@example.com', password='pass1234')
        self.other = CustomUser.objects.create_user(email='selcom-other@example.com', password='pass1234')
        self.setting = ApiSetting.objects.create(
            selcom_enabled=True, selcom_client_id='client-1', selcom_client_secret='secret-1',
        )
        self.invoice = _order_for(self.owner, 'SEL-1', 8001)

    def _initiate(self, email='selcom-owner@example.com'):
        self.client.login(email=email, password='pass1234')
        return self.client.post(reverse('tenders:api_invoice_selcom_initiate', args=[self.invoice.pk]), {})

    def test_initiate_requires_selcom_enabled(self):
        self.setting.selcom_enabled = False
        self.setting.save(update_fields=('selcom_enabled',))
        response = self._initiate()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['ok'])
        self.assertIn('not enabled', response.json()['error'])

    def test_initiate_creates_checkout_order(self):
        with patch('tenders.views.selcom.create_checkout_order', return_value={
            'order_token': 'tok-123', 'pay_link': 'https://checkout/paylink/tok-123',
        }) as mocked:
            response = self._initiate()
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['pay_link'], 'https://checkout/paylink/tok-123')
        mocked.assert_called_once()
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.selcom_order_token, 'tok-123')
        self.assertEqual(self.invoice.selcom_pay_link, 'https://checkout/paylink/tok-123')
        self.assertEqual(self.invoice.selcom_reference, f'{self.invoice.number}-{self.invoice.pk}')

    def test_initiate_denied_for_other_user(self):
        response = self._initiate(email='selcom-other@example.com')
        self.assertEqual(response.status_code, 404)

    def test_status_empty_until_initiated(self):
        self.client.login(email='selcom-owner@example.com', password='pass1234')
        url = reverse('tenders:api_invoice_selcom_status', args=[self.invoice.pk])
        response = self.client.post(url, {})
        data = response.json()
        self.assertFalse(data['ok'])

    def test_status_marks_invoice_paid(self):
        self.invoice.selcom_order_token = 'tok-999'
        self.invoice.save(update_fields=('selcom_order_token',))
        with patch('tenders.views.selcom.get_order_status', return_value={
            'parsed': {'status': 'SUCCESS'}, 'paid': True, 'status': 'SUCCESS',
        }):
            self.client.login(email='selcom-owner@example.com', password='pass1234')
            url = reverse('tenders:api_invoice_selcom_status', args=[self.invoice.pk])
            response = self.client.post(url, {})
        data = response.json()
        self.assertTrue(data['ok'])
        self.assertTrue(data['paid'])
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.Status.PAID)

    def test_webhook_marks_paid_without_secret(self):
        self.invoice.selcom_reference = 'REF-1'
        self.invoice.save(update_fields=('selcom_reference',))
        response = self.client.post(
            reverse('tenders:webhook_selcom'),
            data=json.dumps({'vendor_reference_id': 'REF-1', 'status': 'paid'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['paid'])
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.Status.PAID)

    def test_webhook_rejects_bad_signature(self):
        self.setting.selcom_webhook_secret = 's3cret'
        self.setting.save(update_fields=('selcom_webhook_secret',))
        self.invoice.selcom_reference = 'REF-2'
        self.invoice.save(update_fields=('selcom_reference',))
        response = self.client.post(
            reverse('tenders:webhook_selcom'),
            data=json.dumps({'vendor_reference_id': 'REF-2', 'status': 'paid'}),
            content_type='application/json',
            HTTP_X_SELCOM_SIGNATURE='wrong-signature',
        )
        self.assertEqual(response.status_code, 400)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.Status.PENDING)

    def test_webhook_accepts_valid_signature(self):
        self.setting.selcom_webhook_secret = 's3cret'
        self.setting.save(update_fields=('selcom_webhook_secret',))
        self.invoice.selcom_reference = 'REF-3'
        self.invoice.save(update_fields=('selcom_reference',))
        body = json.dumps({'vendor_reference_id': 'REF-3', 'status': 'paid'}).encode()
        import hmac
        import hashlib
        signature = hmac.new(b's3cret', body, hashlib.sha256).hexdigest()
        response = self.client.post(
            reverse('tenders:webhook_selcom'),
            data=body,
            content_type='application/json',
            HTTP_X_SELCOM_SIGNATURE=signature,
        )
        self.assertEqual(response.status_code, 200)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.Status.PAID)

    def test_settings_page_shows_selcom_card(self):
        self.client.login(email='selcom-admin@example.com', password='pass1234')
        response = self.client.get(reverse('tenders:config_selcom'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Selcom payment gateway')
        self.assertContains(response, 'webhook/selcom')


class MapConfigModelTest(TestCase):
    def test_carto_url_embeds_api_key(self):
        setting = ApiSetting.objects.create(
            map_provider='carto', map_api_key='cb1_3hfb_1_29c6610a1e25efdaaf1dc20d',
        )
        url = setting.map_resolved_tile_url()
        self.assertIn('https://', url)
        self.assertIn('key=cb1_3hfb_1_29c6610a1e25efdaaf1dc20d', url)
        self.assertIn('rastertiles/voyager', url)

    def test_carto_url_without_key_strips_placeholder(self):
        setting = ApiSetting.objects.create(map_provider='carto', map_api_key='')
        url = setting.map_resolved_tile_url()
        self.assertNotIn('key=', url)
        self.assertNotIn('{APIKEY}', url)
        self.assertIn('rastertiles/voyager', url)

    def test_provider_preset_url_substitutes_api_key(self):
        setting = ApiSetting.objects.create(
            map_provider='maptiler', map_api_key='ak-123', map_tile_url='',
        )
        url = setting.map_resolved_tile_url()
        self.assertTrue(url.startswith('https://api.maptiler.com/maps/streets/'))
        self.assertIn('key=ak-123', url)

    def test_custom_tile_url_and_attribution_override(self):
        setting = ApiSetting.objects.create(
            map_provider='custom', map_api_key='AK123',
            map_tile_url='https://tiles.example.com/{z}/{x}/{y}.png?token={APIKEY}',
            map_attribution='&copy; Example tiles',
        )
        self.assertEqual(
            setting.map_resolved_tile_url(),
            'https://tiles.example.com/{z}/{x}/{y}.png?token=AK123',
        )
        self.assertEqual(setting.map_resolved_attribution(), '&copy; Example tiles')


class AdminConfigurationTest(TestCase):
    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            email='admin@example.com', password='pass1234', role=CustomUser.Role.ADMINISTRATOR,
        )
        self.user = CustomUser.objects.create_user(email='user@example.com', password='pass1234')
        self.setting = ApiSetting.objects.create()

    def test_non_admin_redirected_from_config_pages(self):
        self.client.login(email='user@example.com', password='pass1234')
        for name in ('config_odoo', 'config_selcom', 'config_email', 'config_media', 'config_map'):
            response = self.client.get(reverse(f'tenders:{name}'))
            self.assertEqual(response.status_code, 302)

    def test_non_admin_cannot_save_config(self):
        self.client.login(email='user@example.com', password='pass1234')
        response = self.client.post(
            reverse('tenders:config_odoo'),
            {'name': 'Hacked Corp', 'base_url': 'https://hacked.example.com/'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(OdooCompany.objects.filter(name='Hacked Corp').exists())

    def test_odoo_page_renders_empty(self):
        self.client.login(email='admin@example.com', password='pass1234')
        response = self.client.get(reverse('tenders:config_odoo'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'No Odoo companies configured yet')
        self.assertContains(response, 'Add an Odoo company')

    def test_company_create_autogenerates_slug(self):
        self.client.login(email='admin@example.com', password='pass1234')
        response = self.client.post(
            reverse('tenders:config_odoo'),
            {'name': 'ACSC Ltd.', 'base_url': 'https://api.acsc.com/', 'auth_type': 'bearer', 'api_token': 'tok123'},
        )
        self.assertRedirects(response, reverse('tenders:config_odoo'))
        company = OdooCompany.objects.get(name='ACSC Ltd.')
        self.assertEqual(company.slug, 'acsc-ltd')
        self.assertEqual(company.base_url, 'https://api.acsc.com/')
        self.assertEqual(company.auth_type, 'bearer')
        self.assertEqual(company.api_token, 'tok123')

    def test_company_create_uses_provided_slug(self):
        self.client.login(email='admin@example.com', password='pass1234')
        response = self.client.post(
            reverse('tenders:config_odoo'),
            {'name': 'ACSC Ltd.', 'slug': 'acsc-prod', 'base_url': 'https://api.acsc.com/', 'auth_type': 'bearer'},
        )
        self.assertRedirects(response, reverse('tenders:config_odoo'))
        self.assertTrue(OdooCompany.objects.filter(slug='acsc-prod').exists())

    def test_company_slug_unique_with_suffix(self):
        OdooCompany.objects.create(name='ACSC Ltd', base_url='https://a.com')
        self.client.login(email='admin@example.com', password='pass1234')
        self.client.post(
            reverse('tenders:config_odoo'),
            {'name': 'ACSC Ltd', 'base_url': 'https://b.com/', 'auth_type': 'bearer'},
        )
        slugs = [c.slug for c in OdooCompany.objects.filter(name='ACSC Ltd')]
        self.assertEqual(len(slugs), 2)
        self.assertEqual(len(set(slugs)), 2)
        self.assertIn('acsc-ltd', slugs)

    def test_company_edit(self):
        company = OdooCompany.objects.create(name='First', base_url='https://first.com')
        self.client.login(email='admin@example.com', password='pass1234')
        response = self.client.post(
            reverse('tenders:config_odoo'),
            {'id': str(company.pk), 'name': 'Renamed', 'base_url': 'https://renamed.com/', 'auth_type': 'basic', 'username': 'u', 'password': 'p'},
        )
        self.assertRedirects(response, reverse('tenders:config_odoo'))
        company.refresh_from_db()
        self.assertEqual(company.name, 'Renamed')
        self.assertEqual(company.base_url, 'https://renamed.com/')
        self.assertEqual(company.auth_type, 'basic')

    def test_company_delete(self):
        company = OdooCompany.objects.create(name='Doomed', base_url='https://doomed.com')
        self.client.login(email='admin@example.com', password='pass1234')
        response = self.client.post(
            reverse('tenders:config_odoo'), {'delete': str(company.pk)},
        )
        self.assertRedirects(response, reverse('tenders:config_odoo'))
        self.assertFalse(OdooCompany.objects.filter(pk=company.pk).exists())

    def test_odoo_page_hides_endpoint_path_fields(self):
        self.client.login(email='admin@example.com', password='pass1234')
        response = self.client.get(reverse('tenders:config_odoo'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Tenders path')
        self.assertNotContains(response, 'Order confirmation path')
        self.assertNotContains(response, 'Partial order confirmation path')
        self.assertNotContains(response, 'Order invoice path')

    def test_shared_settings_box_removed(self):
        self.setting.tenders_path = '/shared/tenders'
        self.setting.save()
        self.client.login(email='admin@example.com', password='pass1234')
        response = self.client.get(reverse('tenders:config_odoo'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Shared settings')
        self.assertNotContains(response, 'Legacy webhook')
        response = self.client.post(
            reverse('tenders:config_odoo'),
            {'shared': '1', 'base_url': 'https://shared.example.com/'},
        )
        self.assertEqual(response.status_code, 200)
        self.setting.refresh_from_db()
        self.assertNotEqual(self.setting.base_url, 'https://shared.example.com/')
        self.assertEqual(self.setting.tenders_path, '/shared/tenders')

    def test_odoo_page_shows_company_webhook_url(self):
        company = OdooCompany.objects.create(name='Webhook Co', base_url='https://wh.example.com')
        self.client.login(email='admin@example.com', password='pass1234')
        response = self.client.get(reverse('tenders:config_odoo'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Webhook Co')
        webhook = reverse('tenders:webhook_order_company', kwargs={'slug': company.slug})
        self.assertContains(response, webhook)

    def test_selcom_page_renders_and_saves(self):
        self.client.login(email='admin@example.com', password='pass1234')
        response = self.client.get(reverse('tenders:config_selcom'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Payment callback')
        self.assertContains(response, 'webhook/selcom')
        response = self.client.post(
            reverse('tenders:config_selcom'),
            {'selcom_enabled': 'on', 'selcom_currency': 'TZS', 'selcom_client_id': 'cid'},
        )
        self.assertRedirects(response, reverse('tenders:config_selcom'))
        self.setting.refresh_from_db()
        self.assertTrue(self.setting.selcom_enabled)
        self.assertEqual(self.setting.selcom_client_id, 'cid')

    def test_email_page_renders_and_saves(self):
        self.client.login(email='admin@example.com', password='pass1234')
        response = self.client.get(reverse('tenders:config_email'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Incoming email server')
        self.assertContains(response, 'Outgoing email server')
        self.assertContains(response, 'imap.gmail.com')
        self.assertContains(response, 'smtp.gmail.com')
        response = self.client.post(
            reverse('tenders:config_email'),
            {
                'section': 'incoming',
                'email_host': 'imap.gmail.com', 'email_port': '993', 'email_use_ssl': 'on',
                'email_username': 'orders@example.com', 'email_password': 'app-password',
            },
        )
        self.assertRedirects(response, reverse('tenders:config_email'))
        self.setting.refresh_from_db()
        self.assertEqual(self.setting.email_host, 'imap.gmail.com')
        self.assertEqual(self.setting.email_port, 993)
        self.assertTrue(self.setting.email_use_ssl)
        self.assertEqual(self.setting.email_username, 'orders@example.com')
        self.assertEqual(self.setting.email_password, 'app-password')

    def test_email_outgoing_section_saves_smtp(self):
        self.client.login(email='admin@example.com', password='pass1234')
        response = self.client.post(
            reverse('tenders:config_email'),
            {
                'section': 'outgoing',
                'smtp_host': 'smtp.gmail.com', 'smtp_port': '587', 'smtp_use_tls': 'on',
                'smtp_username': 'no-reply@example.com', 'smtp_password': 'smtp-password',
                'email_from': 'HYPAX <no-reply@example.com>',
            },
        )
        self.assertRedirects(response, reverse('tenders:config_email'))
        self.setting.refresh_from_db()
        self.assertEqual(self.setting.smtp_host, 'smtp.gmail.com')
        self.assertEqual(self.setting.smtp_port, 587)
        self.assertTrue(self.setting.smtp_use_tls)
        self.assertFalse(self.setting.smtp_use_ssl)
        self.assertEqual(self.setting.smtp_username, 'no-reply@example.com')
        self.assertEqual(self.setting.smtp_password, 'smtp-password')
        self.assertEqual(self.setting.email_from, 'HYPAX <no-reply@example.com>')

    def test_mail_backend_falls_back_to_console_without_smtp(self):
        backend = ApiSettingEmailBackend(fail_silently=True)
        self.assertIsInstance(backend._get_backend(), ConsoleBackend)
        backend.close()

    def test_mail_backend_uses_smtp_when_configured(self):
        self.setting.smtp_host = 'smtp.gmail.com'
        self.setting.smtp_port = 587
        self.setting.save(update_fields=('smtp_host', 'smtp_port'))
        with patch('DjangoProject.mail_backend.SMTPBackend') as smtp:
            backend = ApiSettingEmailBackend(fail_silently=True)
            backend._get_backend()
            smtp.assert_called_once_with(
                host='smtp.gmail.com', port=587, username='', password='',
                use_tls=True, use_ssl=False, fail_silently=True,
            )
        backend.close()

    def test_config_tabs_have_active_item(self):
        self.client.login(email='admin@example.com', password='pass1234')
        response = self.client.get(reverse('tenders:config_email'))
        self.assertContains(response, 'class="config-tabs"')
        self.assertContains(response, '>Odoo<')
        self.assertContains(response, '>Selcom<')
        self.assertContains(response, '>Email<')
        self.assertContains(response, '>Files<')
        self.assertContains(response, '>Map<')

    def test_media_page_renders_and_saves(self):
        self.client.login(email='admin@example.com', password='pass1234')
        response = self.client.get(reverse('tenders:config_media'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Option A')
        self.assertContains(response, 'Option B')
        response = self.client.post(
            reverse('tenders:config_media'),
            {'media_storage': 's3'},
        )
        self.assertRedirects(response, reverse('tenders:config_media'))
        self.setting.refresh_from_db()
        self.assertEqual(self.setting.media_storage, 's3')

    def test_map_page_renders(self):
        self.client.login(email='admin@example.com', password='pass1234')
        response = self.client.get(reverse('tenders:config_map'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Map provider')
        self.assertContains(response, 'API key')

    def test_map_page_saves_and_resolves_url(self):
        self.client.login(email='admin@example.com', password='pass1234')
        response = self.client.post(
            reverse('tenders:config_map'),
            {
                'map_provider': 'maptiler', 'map_api_key': 'tile-key-1',
                'map_tile_url': '', 'map_attribution': '', 'map_max_zoom': '20',
            },
        )
        self.assertRedirects(response, reverse('tenders:config_map'))
        self.setting.refresh_from_db()
        self.assertEqual(self.setting.map_provider, 'maptiler')
        self.assertEqual(self.setting.map_api_key, 'tile-key-1')
        self.assertEqual(self.setting.map_max_zoom, 20)
        url = self.setting.map_resolved_tile_url()
        self.assertIn('https://api.maptiler.com/maps/streets/', url)
        self.assertIn('key=tile-key-1', url)

    def test_map_page_custom_url(self):
        self.client.login(email='admin@example.com', password='pass1234')
        response = self.client.post(
            reverse('tenders:config_map'),
            {
                'map_provider': 'custom', 'map_api_key': 'k', 'map_max_zoom': '18',
                'map_tile_url': 'https://cdngeo.example.com/{z}/{x}/{y}.png?token={APIKEY}',
                'map_attribution': '&copy; CdnGeo',
            },
        )
        self.assertRedirects(response, reverse('tenders:config_map'))
        self.setting.refresh_from_db()
        self.assertEqual(
            self.setting.map_resolved_tile_url(),
            'https://cdngeo.example.com/{z}/{x}/{y}.png?token=k',
        )
        self.assertEqual(self.setting.map_resolved_attribution(), '&copy; CdnGeo')


class OdooCompanyWebhookTest(TestCase):
    def setUp(self):
        self.owner = CustomUser.objects.create_user(
            email='webhook-owner@example.com', password='pass1234',
        )
        self.company_a = OdooCompany.objects.create(name='Alpha Co', base_url='https://alpha.example.com')
        self.company_b = OdooCompany.objects.create(name='Beta Co', base_url='https://beta.example.com', is_active=False)
        self.client.login(email='webhook-owner@example.com', password='pass1234')

    def _tender(self, ref):
        return Tender.objects.create(
            user=self.owner, route_loading='Nairobi', route_delivery='Mombasa',
            customer=f'C-{ref}', cargo_type=Tender.CargoType.DRY_VAN,
            truck_type=Tender.TruckType.TRUCK, weight=10.0, number_of_trucks=1,
            distance_km=480, cargo_date=timezone.localdate(), cargo_reference=ref,
        )

    def _payload(self, order_id, ref):
        return {
            'order_id': order_id, 'order_name': f'ORD-{order_id}', 'state': 'done',
            'company_id': 1, 'company_name': 'Alpha Co', 'cargo_reference': ref,
            'date_order': '2026-09-01 10:00:00', 'amount_total': 150.0,
            'order_lines': [{'line_id': 1, 'product_name': 'Sand', 'quantity': 1,
                             'commission': 0, 'price_unit': 150, 'price_subtotal': 150, 'price_total': 150}],
        }

    def test_webhook_requires_slug(self):
        self._tender('REF-L')
        response = self.client.post(
            '/webhook/orders/',
            json.dumps(self._payload(90001, 'REF-L')), content_type='application/json',
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Order.objects.filter(order_id=90001).exists())
        with self.assertRaises(NoReverseMatch):
            reverse('tenders:webhook_orders')

    def test_webhook_company_path_attaches_company(self):
        self._tender('REF-A')
        url = reverse('tenders:webhook_order_company', kwargs={'slug': self.company_a.slug})
        response = self.client.post(url, json.dumps(self._payload(90002, 'REF-A')), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        order = Order.objects.get(order_id=90002)
        self.assertEqual(order.odoo_company, self.company_a)

    def test_webhook_unknown_slug_returns_404(self):
        response = self.client.post(
            reverse('tenders:webhook_order_company', kwargs={'slug': 'nope'}),
            json.dumps(self._payload(90003, 'REF-X')), content_type='application/json',
        )
        self.assertEqual(response.status_code, 404)

    def test_webhook_inactive_company_return_403(self):
        url = reverse('tenders:webhook_order_company', kwargs={'slug': self.company_b.slug})
        response = self.client.post(url, json.dumps(self._payload(90004, 'REF-X')), content_type='application/json')
        self.assertEqual(response.status_code, 403)

    def test_webhook_links_order_by_tender_reference(self):
        tender = Tender.objects.create(
            user=self.owner, route_loading='Nairobi', route_delivery='Mombasa',
            customer='RefCo', cargo_type=Tender.CargoType.DRY_VAN,
            truck_type=Tender.TruckType.TRUCK, weight=10.0, number_of_trucks=1,
            distance_km=480, cargo_date=timezone.localdate(),
            reference='HYPAX-000777',
        )
        url = reverse('tenders:webhook_order_company', kwargs={'slug': self.company_a.slug})
        response = self.client.post(
            url, json.dumps(self._payload(90005, 'HYPAX-000777')), content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['linked_tender'], 'HYPAX-000777')
        order = Order.objects.get(order_id=90005)
        self.assertEqual(order.tender, tender)
        self.assertEqual(order.cargo_reference, 'HYPAX-000777')


class OdooCompanyRoutingTest(TestCase):
    def setUp(self):
        self.owner = CustomUser.objects.create_user(
            email='route-owner@example.com', password='pass1234',
        )
        self.admin = CustomUser.objects.create_user(
            email='route-admin@example.com', password='pass1234',
            role=CustomUser.Role.ADMINISTRATOR,
        )
        self.company = OdooCompany.objects.create(
            name='Routing Co', base_url='https://routing.example.com',
            auth_type='bearer', api_token='tok-route',
        )
        ApiSetting.objects.create(base_url='https://shared.example.com/')

    def test_company_paths_default_when_blank(self):
        self.assertEqual(self.company.endpoint_url(), 'https://routing.example.com/api/v1/tenders')
        self.assertEqual(self.company.order_confirmation_url(), 'https://routing.example.com/api/v1/order-confirmation')
        self.assertEqual(self.company.partial_order_confirmation_url(), 'https://routing.example.com/api/v1/partial-order-confirmation')
        self.assertEqual(self.company.order_invoice_url(), 'https://routing.example.com/api/v1/order-invoice')

    def test_company_paths_use_custom_values(self):
        self.company.tenders_path = '/post/tenders'
        self.company.order_confirmation_path = 'confirm'
        self.company.partial_order_confirmation_path = '/partial'
        self.company.order_invoice_path = '/paid-invoice'
        self.company.save()
        self.assertEqual(self.company.endpoint_url(), 'https://routing.example.com/post/tenders')
        self.assertEqual(self.company.order_confirmation_url(), 'https://routing.example.com/confirm')
        self.assertEqual(self.company.partial_order_confirmation_url(), 'https://routing.example.com/partial')
        self.assertEqual(self.company.order_invoice_url(), 'https://routing.example.com/paid-invoice')

    def _order(self, order_id, ref, company=None):
        tender = self._tender(ref)
        order = Order.objects.create(
            order_id=order_id, order_name=f'ORD-{order_id}', user=self.owner, tender=tender,
            company_id=2, company_name='Routing Co', cargo_reference=ref, state='confirmed',
            amount_total=0, currency='TZS', odoo_company=company,
        )
        OrderLine.objects.create(
            order=order, line_id=order_id, product_name='Sand', quantity=1,
            price_unit=100, commission=0, price_subtotal=100, price_total=100, awarded=True,
        )
        return order

    def _tender(self, ref):
        return Tender.objects.create(
            user=self.owner, route_loading='Nairobi', route_delivery='Mombasa',
            customer=f'C-{ref}', cargo_type=Tender.CargoType.DRY_VAN,
            truck_type=Tender.TruckType.TRUCK, weight=10.0, number_of_trucks=1,
            distance_km=480, cargo_date=timezone.localdate(), cargo_reference=ref,
        )

    def test_award_confirmation_posts_to_order_company(self):
        order = self._order(91001, 'REF-ROUTE', company=self.company)
        self.client.login(email='route-owner@example.com', password='pass1234')
        with patch('tenders.views.submit_confirmation',
                   return_value=(200, '{"status":"success"}', True)) as m:
            response = self.client.post(reverse('tenders:api_order_award', args=[order.pk]), {})
        self.assertTrue(response.json()['ok'])
        url = m.call_args[0][1]
        self.assertEqual(url, self.company.order_confirmation_url())

    def test_award_confirmation_without_company_is_rejected(self):
        order = self._order(91002, 'REF-FALLBACK', company=None)
        self.client.login(email='route-owner@example.com', password='pass1234')
        with patch('tenders.views.submit_confirmation',
                   return_value=(200, '{"status":"success"}', True)) as m:
            response = self.client.post(reverse('tenders:api_order_award', args=[order.pk]), {})
        self.assertFalse(response.json()['ok'])
        self.assertIn('no Odoo company', response.json()['error'])
        m.assert_not_called()

    def test_invoice_confirmation_posts_to_order_company(self):
        from tenders.views import get_or_create_invoice
        order = self._order(91003, 'REF-INV', company=self.company)
        invite = get_or_create_invoice(order)
        self.client.login(email='route-admin@example.com', password='pass1234')
        with patch('tenders.views.submit_confirmation',
                   return_value=(200, ('{"status":"success","data":{"name":"EXT-INV-1"}}'), True)) as m:
            response = self.client.post(reverse('tenders:api_invoice_paid', args=[invite.pk]), {})
        self.assertTrue(response.json()['ok'])
        url = m.call_args[0][1]
        self.assertEqual(url, self.company.order_invoice_url())

    def test_invoice_confirmation_without_company_is_rejected(self):
        from tenders.views import get_or_create_invoice
        order = self._order(91004, 'REF-INV-FALLBACK', company=None)
        invite = get_or_create_invoice(order)
        self.client.login(email='route-admin@example.com', password='pass1234')
        with patch('tenders.views.submit_confirmation',
                   return_value=(200, ('{"status":"success","data":{"name":"EXT-INV-2"}}'), True)) as m:
            response = self.client.post(reverse('tenders:api_invoice_paid', args=[invite.pk]), {})
        self.assertFalse(response.json()['ok'])
        self.assertIn('no Odoo company', response.json()['error'])
        m.assert_not_called()


class ApiDiagnosticsTest(TestCase):
    """Every error response from the platform's API points is recorded for the admin Diagnostics page."""

    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            email='diag-admin@example.com', password='pass1234',
            role=CustomUser.Role.ADMINISTRATOR,
        )
        self.user = CustomUser.objects.create_user(
            email='diag-user@example.com', password='pass1234',
        )
        self.company = OdooCompany.objects.create(
            name='Diag Transporter', base_url='https://diag.example.com/', auth_type='bearer', api_token='tok',
        )

    def _login(self, user):
        self.client.force_login(user)

    def test_admin_page_requires_admin(self):
        self._login(self.user)
        response = self.client.get(reverse('tenders:admin_diagnostic'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], reverse('tenders:dashboard'))

        self.client.logout()
        response = self.client.get(reverse('tenders:admin_diagnostic'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login', response['Location'])

    def test_admin_page_and_api(self):
        from tenders.models import ApiDiagnostic
        ApiDiagnostic.objects.create(
            user=self.user, api_point='town_route', method='GET', path='/api/town-route/',
            status_code=400, message='Both "from" and "to" parameters are required.',
        )
        self._login(self.admin)
        response = self.client.get(reverse('tenders:admin_diagnostic'))
        self.assertTemplateUsed(response, 'tenders/admin_diagnostics.html')

        data = self.client.get(reverse('tenders:api_admin_diagnostic')).json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['total'], 1)
        entry = data['diagnostics'][0]
        self.assertEqual(entry['api_point'], 'town_route')
        self.assertEqual(entry['status_code'], 400)
        self.assertEqual(entry['user']['email'], self.user.email)
        self.assertEqual(entry['message'], 'Both "from" and "to" parameters are required.')
        self.assertEqual(data['api_points'], ['town_route'])
        self.assertEqual(data['today_count'], 1)

    def test_middleware_logs_api_error_response(self):
        from tenders.models import ApiDiagnostic
        self._login(self.user)
        response = self.client.get(reverse('tenders:api_town_route'), {'from': 'Nairobi'})
        self.assertEqual(response.status_code, 400)
        entry = ApiDiagnostic.objects.get(api_point='town_route')
        self.assertEqual(entry.user, self.user)
        self.assertEqual(entry.status_code, 400)
        self.assertEqual(entry.method, 'GET')
        self.assertEqual(entry.path, reverse('tenders:api_town_route'))
        self.assertEqual(entry.message, 'Both "from" and "to" parameters are required.')

    def test_ok_false_response_logged_once(self):
        from tenders.models import ApiDiagnostic
        ApiSetting.objects.create(base_url='https://diag.example.com/')
        order = Order.objects.create(
            order_id=99001, order_name='ORD-99001', user=self.user, state='draft',
            amount_total=0, currency='TZS', cargo_reference='',
        )
        self._login(self.user)
        response = self.client.post(reverse('tenders:api_order_award', args=[order.pk]), {})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['ok'])
        entries = ApiDiagnostic.objects.filter(api_point='order_award')
        self.assertEqual(entries.count(), 1)
        self.assertEqual(entries[0].user, self.user)
        self.assertIn('no Odoo company', entries[0].message)

    def test_award_confirmation_failure_logged(self):
        from tenders.models import ApiDiagnostic
        tender = Tender.objects.create(
            user=self.user, route_loading='Nairobi', route_delivery='Mombasa',
            customer='DiagCo', cargo_type=Tender.CargoType.DRY_VAN,
            truck_type=Tender.TruckType.TRUCK, weight=10.0, number_of_trucks=1,
            distance_km=480, cargo_date=timezone.localdate(), cargo_reference='REF-DIAG',
        )
        order = Order.objects.create(
            order_id=99002, order_name='ORD-99002', user=self.user, state='draft',
            tender=tender, cargo_reference='REF-DIAG', amount_total=0, currency='TZS',
            odoo_company=self.company,
        )
        self._login(self.user)
        with patch('tenders.views.submit_confirmation', return_value=(500, 'server boom', False)):
            response = self.client.post(reverse('tenders:api_order_award', args=[order.pk]), {})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['ok'])
        entries = ApiDiagnostic.objects.filter(api_point='order.award')
        self.assertEqual(entries.count(), 1)
        self.assertIn('HTTP 500', entries[0].message)
        self.assertEqual(entries[0].path, 'https://diag.example.com/api/v1/order-confirmation')
        self.assertEqual(entries[0].status_code, 500)

    def test_tender_submit_failure_logged(self):
        from tenders.models import ApiDiagnostic
        ApiSetting.objects.create(base_url='https://diag.example.com/')
        self._login(self.user)
        with patch('tenders.views.submit_tender', return_value=(500, 'external error', False)):
            response = self.client.post(
                reverse('tenders:api_tender_create'),
                json.dumps({
                    'route_loading': 'Nairobi',
                    'route_delivery': 'Mombasa',
                    'customer': 'DiagCo',
                    'cargo_type': 'dry_van',
                    'truck_type': 'truck',
                    'weight': 10.0,
                    'number_of_trucks': 1,
                    'distance_km': 480.0,
                    'cargo_date': timezone.localdate().isoformat(),
                }),
                content_type='application/json',
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        entry = ApiDiagnostic.objects.get(api_point='tender.submit')
        self.assertEqual(entry.user, self.user)
        self.assertEqual(entry.status_code, 500)
        self.assertEqual(entry.path, 'https://diag.example.com/api/v1/tenders')
        self.assertIn('HTTP 500', entry.message)
        self.assertEqual(entry.detail['response'], 'external error')

    def test_webhook_error_logged_without_user(self):
        from tenders.models import ApiDiagnostic
        response = self.client.post(
            reverse('tenders:webhook_order_company', kwargs={'slug': 'nope'}),
            json.dumps({'order_id': 1}), content_type='application/json',
        )
        self.assertEqual(response.status_code, 404)
        entry = ApiDiagnostic.objects.get(status_code=404)
        self.assertIsNone(entry.user)
        self.assertEqual(entry.path, reverse('tenders:webhook_order_company', kwargs={'slug': 'nope'}))
        self.assertEqual(entry.method, 'POST')

    def test_api_diagnostic_filter_by_point_and_search(self):
        from tenders.models import ApiDiagnostic
        other = CustomUser.objects.create_user(email='other@example.com', password='pass1234')
        ApiDiagnostic.objects.create(user=self.user, api_point='order.award', message='Awards boom')
        ApiDiagnostic.objects.create(user=other, api_point='tender.submit', message='Submit boom')
        self._login(self.admin)
        data = self.client.get(
            reverse('tenders:api_admin_diagnostic'), {'api_point': 'tender.submit'},
        ).json()
        self.assertEqual(data['total'], 1)
        self.assertEqual(data['diagnostics'][0]['message'], 'Submit boom')

        data = self.client.get(
            reverse('tenders:api_admin_diagnostic'), {'q': 'diag-user@example.com'},
        ).json()
        self.assertEqual(data['total'], 1)
        self.assertEqual(data['diagnostics'][0]['api_point'], 'order.award')
        self.assertEqual(
            data['emails'], ['diag-user@example.com', 'other@example.com'],
        )


class SubmitRetryTest(TestCase):
    """Outgoing HTTP calls retry on connection errors and report friendly messages."""

    def setUp(self):
        self.user = CustomUser.objects.create_user(email='retry@example.com', password='pass1234')
        self.setting = ApiSetting.objects.create(base_url='https://flaky.example.com/')
        self.tender = Tender.objects.create(
            user=self.user, route_loading='Nairobi', route_delivery='Mombasa',
            customer='RetryCo', cargo_type=Tender.CargoType.DRY_VAN,
            truck_type=Tender.TruckType.TRUCK, weight=10.0, number_of_trucks=1,
            distance_km=480, cargo_date=timezone.localdate(),
        )

    def _response(self, status=200, text='ok'):
        resp = Mock()
        resp.status_code = status
        resp.text = text
        resp.ok = status < 400
        return resp

    def test_retries_once_then_succeeds(self):
        with patch('tenders.views.http.post', side_effect=[
            requests.ConnectionError('[Errno 111] Connection refused'),
            self._response(200, '{"status":"success"}'),
        ]) as m:
            status, body, ok = tenders_views.submit_tender(self.setting, self.tender)
        self.assertEqual(m.call_count, 2)
        self.assertEqual(status, 200)
        self.assertTrue(ok)
        self.assertIn('success', body)

    def test_connection_error_exhausts_retries(self):
        with patch('tenders.views.http.post',
                   side_effect=requests.ConnectionError('[Errno 111] Connection refused')) as m:
            status, body, ok = tenders_views.submit_tender(self.setting, self.tender)
        self.assertEqual(m.call_count, 2)
        self.assertIsNone(status)
        self.assertFalse(ok)
        self.assertIn('[Errno 111]', body)

    def test_http_error_not_retried(self):
        with patch('tenders.views.http.post', return_value=self._response(500, 'boom')) as m:
            status, body, ok = tenders_views.submit_tender(self.setting, self.tender)
        self.assertEqual(m.call_count, 1)
        self.assertEqual(status, 500)
        self.assertFalse(ok)

    def test_friendly_connection_refused_message(self):
        message = tenders_views._submit_failure_message(
            'Tender submission', None, "HTTPSConnectionPool(...): Max retries exceeded ... [Errno 111] Connection refused",
        )
        self.assertIn('connection refused', message)
        self.assertIn('instance is online', message)
        self.assertNotIn('HTTP None', message)

    def test_friendly_timeout_message(self):
        message = tenders_views._submit_failure_message('Order confirmation', None, '[Errno 110] Connection timed out')
        self.assertIn('connection timed out', message)


class TenderBroadcastTest(TestCase):
    """Every tender is sent to ALL configured transport companies (Odoo instances).

    Registered companies are *customer* companies (who we ship cargo for) and carry
    no transporter link. The tender form has no transport-company field: the tender
    goes to every transport company whose base URL is configured.
    """

    def setUp(self):
        self.user = CustomUser.objects.create_user(email='tender.co@example.com', password='pass1234')
        self.client.login(email='tender.co@example.com', password='pass1234')

    def _payload(self):
        return {
            'route_loading': 'Nairobi',
            'route_delivery': 'Mombasa',
            'customer': 'TransCo',
            'cargo_type': 'dry_van',
            'truck_type': 'truck',
            'weight': 10.0,
            'number_of_trucks': 1,
            'distance_km': 480.0,
            'cargo_date': timezone.localdate().isoformat(),
        }

    def _post_tender(self):
        return self.client.post(
            reverse('tenders:api_tender_create'), json.dumps(self._payload()),
            content_type='application/json',
        )

    def test_tender_sent_to_sole_configured_transport_company(self):
        odoo = OdooCompany.objects.create(
            name='LAKE TRANS', base_url='https://lake.example.com', auth_type='bearer', api_token='tok',
        )
        with patch('tenders.views.submit_tender',
                   return_value=(200, '{"status":"success","data":{"id":5,"name":"CAR0001"}}', True)) as m:
            response = self._post_tender()
        self.assertTrue(response.json()['ok'])
        self.assertNotIn('needs_settings', response.json())
        self.assertEqual(m.call_count, 1)
        self.assertEqual(m.call_args[0][0].pk, odoo.pk)
        self.assertEqual(m.call_args[0][0].endpoint_url(), 'https://lake.example.com/api/v1/tenders')
        self.assertIn('1 of 1', response.json()['message'])

    def test_tender_sent_to_all_configured_transport_companies(self):
        first = OdooCompany.objects.create(name='Lake Trans', base_url='https://lake.example.com')
        second = OdooCompany.objects.create(
            name='Second Trans', base_url='https://second.example.com', tenders_path='/dispatch/tender',
        )
        with patch('tenders.views.submit_tender',
                   return_value=(200, '{"status":"success","data":{"id":6,"name":"CAR0002"}}', True)) as m:
            response = self._post_tender()
        self.assertTrue(response.json()['ok'])
        self.assertEqual(m.call_count, 2)
        sent_to = {c.args[0].pk for c in m.call_args_list}
        self.assertEqual(sent_to, {first.pk, second.pk})
        endpoints = {c.args[0].endpoint_url() for c in m.call_args_list}
        self.assertEqual(
            endpoints,
            {'https://lake.example.com/api/v1/tenders', 'https://second.example.com/dispatch/tender'},
        )
        self.assertIn('2 of 2', response.json()['message'])

    def test_inactive_or_url_missing_transporters_are_skipped(self):
        OdooCompany.objects.create(name='Active', base_url='https://active.example.com')
        OdooCompany.objects.create(name='No URL', base_url='')
        OdooCompany.objects.create(name='Inactive', base_url='https://inactive.example.com', is_active=False)
        with patch('tenders.views.submit_tender',
                   return_value=(200, '{"status":"success","data":{"id":7,"name":"CAR0007"}}', True)) as m:
            response = self._post_tender()
        self.assertTrue(response.json()['ok'])
        self.assertEqual(m.call_count, 1)
        self.assertEqual(m.call_args[0][0].name, 'Active')

    def test_submissions_recorded_per_transport_company(self):
        OdooCompany.objects.create(name='Lake Trans', base_url='https://lake.example.com')
        second = OdooCompany.objects.create(
            name='Second Trans', base_url='https://second.example.com', tenders_path='/dispatch/tender',
        )
        with patch('tenders.views.submit_tender',
                   return_value=(200, '{"status":"success","data":{"id":56,"name":"CAR0056"}}', True)) as m:
            response = self._post_tender()
        self.assertTrue(response.json()['ok'])
        tender_data = response.json()['tender']
        self.assertEqual(len(tender_data['odoo_companies']), 2)
        self.assertEqual({c['name'] for c in tender_data['odoo_companies']}, {'Lake Trans', 'Second Trans'})
        tender = Tender.objects.get(pk=tender_data['id'])
        subs = tender.submissions.select_related('odoo_company').order_by('odoo_company__name')
        self.assertEqual(subs.count(), 2)
        self.assertTrue(all(s.success for s in subs))
        self.assertEqual({s.cargo_reference for s in subs}, {'CAR0056'})
        self.assertEqual(subs.filter(odoo_company=second).count(), 1)
        self.assertEqual(tender.cargo_reference, 'CAR0056')

    def test_tender_reference_generated_at_creation(self):
        with patch('tenders.views.submit_tender') as m:
            response = self._post_tender()
        self.assertTrue(response.json()['needs_settings'])
        m.assert_not_called()
        tender = Tender.objects.get(customer='TransCo')
        self.assertEqual(tender.cargo_reference, '')
        self.assertTrue(tender.reference.startswith(Tender.REFERENCE_PREFIX))
        self.assertEqual(
            tender.reference,
            Tender.make_reference(tender.pk, tender.created_at),
        )
        self.assertRegex(tender.reference, r'^HX-\d{4}-\d{6}$')

    def test_tender_reference_generated_when_sent(self):
        OdooCompany.objects.create(name='Lake Trans', base_url='https://lake.example.com')
        with patch('tenders.views.submit_tender',
                   return_value=(200, '{"status":"success","data":{"id":5,"name":"CAR0001"}}', True)):
            response = self._post_tender()
        self.assertTrue(response.json()['ok'])
        tender = Tender.objects.get(customer='TransCo')
        self.assertTrue(tender.reference.startswith(Tender.REFERENCE_PREFIX))
        self.assertEqual(
            tender.reference,
            Tender.make_reference(tender.pk, tender.created_at),
        )

    def test_tender_reference_is_unique_per_tender(self):
        OdooCompany.objects.create(name='Lake Trans', base_url='https://lake.example.com')
        with patch('tenders.views.submit_tender', return_value=(200, '{"status":"success"}', True)):
            self._post_tender()
            self._post_tender()
        refs = list(Tender.objects.values_list('reference', flat=True))
        self.assertTrue(all(refs))
        self.assertEqual(len(refs), len(set(refs)))

    def test_reference_is_stable_across_retries(self):
        OdooCompany.objects.create(name='Lake Trans', base_url='https://lake.example.com')
        with patch('tenders.views.submit_tender', return_value=(200, '{"status":"success"}', True)):
            self._post_tender()
        tender = Tender.objects.get(customer='TransCo')
        first = tender.reference
        tender.ensure_reference()
        tender.refresh_from_db()
        self.assertEqual(tender.reference, first)

    def test_build_payload_sends_reference_as_cargo_reference(self):
        odoo = OdooCompany.objects.create(name='Lake Trans', base_url='https://lake.example.com')
        tender = Tender.objects.create(
            user=self.user, route_loading='Nairobi', route_delivery='Mombasa',
            customer='TransCo', cargo_type=Tender.CargoType.DRY_VAN,
            truck_type=Tender.TruckType.TRUCK, weight=10.0, number_of_trucks=1,
            distance_km=480, cargo_date=timezone.localdate(),
        )
        tender.ensure_reference()
        with patch('tenders.views._post_with_retry',
                   return_value=(200, '{"status":"success"}', True)) as m:
            status, body, ok = tenders_views.submit_tender(odoo, tender)
        self.assertTrue(ok)
        payload = m.call_args[0][1]
        self.assertEqual(payload['tender_reference'], tender.reference)
        self.assertEqual(payload['cargo_reference'], tender.reference)

    def test_webhook_links_order_by_any_submission_reference(self):
        first = OdooCompany.objects.create(name='Lake Trans', base_url='https://lake.example.com')
        second = OdooCompany.objects.create(name='Second Trans', base_url='https://second.example.com')

        def send_tender(setting, tender):
            ref = 'CAR0001' if setting.pk == first.pk else 'CAR0099'
            return (200, '{"status":"success","data":{"id":1,"name":"%s"}}' % ref, True)

        with patch('tenders.views.submit_tender', side_effect=send_tender):
            self._post_tender()
        tender = Tender.objects.get(customer='TransCo')
        self.assertEqual(tender.cargo_reference, 'CAR0001')
        response = self.client.post(
            reverse('tenders:webhook_order_company', kwargs={'slug': second.slug}),
            json.dumps({
                'order_id': 55001, 'order_name': 'ORD-0001', 'cargo_reference': 'CAR0099',
                'amount_total': 1200.0, 'currency': 'USD',
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['linked_tender'], tender.reference)
        order = Order.objects.get(order_id=55001)
        self.assertEqual(order.tender, tender)
        self.assertEqual(order.cargo_reference, 'CAR0099')

    def test_tender_success_when_any_transporter_succeeds(self):
        OdooCompany.objects.create(name='Down', base_url='https://down.example.com')
        up = OdooCompany.objects.create(name='Up', base_url='https://up.example.com')

        def send(setting, tender):
            if setting.pk == up.pk:
                return (200, '{"status":"success","data":{"id":9,"name":"CAR0009"}}', True)
            return (503, 'Service Unavailable', False)

        with patch('tenders.views.submit_tender', side_effect=send):
            response = self._post_tender()
        self.assertTrue(response.json()['ok'])
        self.assertIn('1 of 2', response.json()['message'])
        self.assertTrue(response.json()['queued'])
        tender = Tender.objects.get(customer='TransCo')
        self.assertEqual(tender.status, Tender.Status.SUCCESS)
        self.assertEqual(tender.cargo_reference, 'CAR0009')
        self.assertTrue(tender.submissions.filter(odoo_company=up, success=True).exists())
        self.assertTrue(tender.submissions.filter(odoo_company__name='Down', success=False).exists())
        self.assertEqual(tender.pending_pushes.filter(state='pending').count(), 1)

    def test_tender_not_submitted_without_configured_transport_company(self):
        # A shared platform ApiSetting row is NOT a tender target.
        ApiSetting.objects.create(base_url='https://shared.example.com/')
        with patch('tenders.views.submit_tender') as m:
            response = self._post_tender()
        self.assertTrue(response.json()['ok'])
        self.assertTrue(response.json()['needs_settings'])
        m.assert_not_called()

    def test_tender_saved_locally_when_no_target(self):
        with patch('tenders.views.submit_tender') as m:
            response = self._post_tender()
        self.assertTrue(response.json()['ok'])
        self.assertTrue(response.json()['needs_settings'])
        m.assert_not_called()

    def test_same_reference_sent_to_every_transporter(self):
        OdooCompany.objects.create(name='Lake Trans', base_url='https://lake.example.com')
        OdooCompany.objects.create(name='Second Trans', base_url='https://second.example.com')
        with patch('tenders.views._post_with_retry',
                   return_value=(200, '{"status":"success","data":{"id":6,"name":"CAR0002"}}', True)) as m:
            response = self._post_tender()
        self.assertTrue(response.json()['ok'])
        tender = Tender.objects.get(customer='TransCo')
        self.assertEqual(len(m.call_args_list), 2)
        refs = {args[0][1]['cargo_reference'] for args in m.call_args_list}
        self.assertEqual(refs, {tender.reference})

    def test_order_confirmation_uses_canonical_tender_reference(self):
        odoo = OdooCompany.objects.create(name='Lake Trans', base_url='https://lake.example.com')
        tender = Tender.objects.create(
            user=self.user, route_loading='Nairobi', route_delivery='Mombasa',
            customer='ConfirmCo', cargo_type=Tender.CargoType.DRY_VAN,
            truck_type=Tender.TruckType.TRUCK, weight=10.0, number_of_trucks=1,
            distance_km=480, cargo_date=timezone.localdate(),
        )
        tender.ensure_reference()
        order = Order.objects.create(
            order_id=77001, order_name='ORD-77001', user=self.user, tender=tender,
            company_id=1, company_name='Lake Trans', cargo_reference='CAR9999',
            state='confirmed', amount_total=0, currency='USD', odoo_company=odoo,
        )
        self.assertNotEqual(tender.reference, order.cargo_reference)
        with patch('tenders.views.submit_confirmation',
                   return_value=(200, '{"status":"success"}', True)) as m:
            response = self.client.post(reverse('tenders:api_order_award', args=[order.pk]), {})
        self.assertTrue(response.json()['ok'])
        payload = m.call_args[0][2]
        self.assertEqual(payload['cargo_name'], tender.reference)
        self.assertEqual(payload['tender_reference'], tender.reference)

    def test_partial_order_confirmation_includes_tender_reference(self):
        odoo = OdooCompany.objects.create(name='Lake Trans', base_url='https://lake.example.com')
        tender = Tender.objects.create(
            user=self.user, route_loading='Nairobi', route_delivery='Mombasa',
            customer='PartialCo', cargo_type=Tender.CargoType.DRY_VAN,
            truck_type=Tender.TruckType.TRUCK, weight=10.0, number_of_trucks=1,
            distance_km=480, cargo_date=timezone.localdate(),
        )
        tender.ensure_reference()
        order = Order.objects.create(
            order_id=77002, order_name='ORD-77002', user=self.user, tender=tender,
            company_id=1, company_name='Lake Trans', cargo_reference='CAR8888',
            state='confirmed', amount_total=0, currency='USD', odoo_company=odoo,
        )
        OrderLine.objects.create(
            order=order, line_id=1, product_name='Sand', quantity=1,
            price_unit=100, commission=0, price_subtotal=100, price_total=100,
        )
        with patch('tenders.views.submit_confirmation',
                   return_value=(200, '{"status":"success"}', True)) as m:
            response = self.client.post(
                reverse('tenders:api_order_award', args=[order.pk]),
                json.dumps({'line_ids': [1]}), content_type='application/json',
            )
        self.assertTrue(response.json()['ok'])
        payload = m.call_args[0][2]
        self.assertEqual(payload['tender_reference'], tender.reference)
        self.assertEqual(payload['cargo_name'], tender.reference)
        self.assertEqual(payload['order_lines'], [{'line_id': 1}])


class PendingPushTest(TestCase):
    """Failed tender submissions are queued and re-sent when the Odoo API is reachable again."""

    def setUp(self):
        from tenders.models import PendingPush
        self.pending_model = PendingPush
        self.user = CustomUser.objects.create_user(email='queue@example.com', password='pass1234')
        self.client.login(email='queue@example.com', password='pass1234')
        self.admin = CustomUser.objects.create_user(
            email='queueadmin@example.com', password='pass1234',
            role=CustomUser.Role.ADMINISTRATOR,
        )
        self.setting = ApiSetting.objects.create(base_url='https://queue.example.com/')
        self.odoo = OdooCompany.objects.create(
            name='Queue Transporter', base_url='https://queue.example.com/', auth_type='bearer',
        )

    def _payload(self):
        return {
            'route_loading': 'Nairobi',
            'route_delivery': 'Mombasa',
            'customer': 'QueueCo',
            'cargo_type': 'dry_van',
            'truck_type': 'truck',
            'weight': 10.0,
            'number_of_trucks': 1,
            'distance_km': 480.0,
            'cargo_date': timezone.localdate().isoformat(),
        }

    def _post_tender(self):
        return self.client.post(
            reverse('tenders:api_tender_create'), json.dumps(self._payload()),
            content_type='application/json',
        )

    def _enqueue(self):
        tender = Tender.objects.create(
            user=self.user, route_loading='Nairobi', route_delivery='Mombasa',
            customer='QueueCo', cargo_type=Tender.CargoType.DRY_VAN,
            truck_type=Tender.TruckType.TRUCK, weight=10.0, number_of_trucks=1,
            distance_km=480, cargo_date=timezone.localdate(), status=Tender.Status.FAILED,
        )
        return tenders_views._enqueue_tender(tender, 'initial failure')

    def test_transient_failure_is_queued(self):
        with patch('tenders.views.submit_tender',
                   return_value=(None, 'HTTPSConnectionPool(...): [Errno 111] Connection refused', False)):
            response = self._post_tender()
        data = response.json()
        self.assertTrue(data['queued'])
        self.assertIn('queued', data['message'])
        push = self.pending_model.objects.get()
        self.assertEqual(push.state, self.pending_model.State.PENDING)
        self.assertEqual(push.tender.status, Tender.Status.FAILED)

    def test_http_5xx_failure_is_queued(self):
        with patch('tenders.views.submit_tender', return_value=(503, 'Service Unavailable', False)):
            response = self._post_tender()
        self.assertTrue(response.json()['queued'])
        self.assertEqual(self.pending_model.objects.count(), 1)

    def test_http_4xx_failure_not_queued(self):
        with patch('tenders.views.submit_tender', return_value=(400, '{"message":"bad request"}', False)):
            response = self._post_tender()
        data = response.json()
        self.assertNotIn('queued', data)
        self.assertEqual(self.pending_model.objects.count(), 0)

    def test_flush_delivers_pending_push(self):
        push = self._enqueue()
        with patch('tenders.views.submit_tender',
                   return_value=(200, '{"status":"success","data":{"id":9,"name":"CAR0009"}}', True)):
            delivered, failed, skipped = tenders_views.flush_pending_pushes()
        self.assertEqual((delivered, failed, skipped), (1, 0, 0))
        push.refresh_from_db()
        self.assertEqual(push.state, self.pending_model.State.DELIVERED)
        self.assertIsNotNone(push.delivered_at)
        push.tender.refresh_from_db()
        self.assertEqual(push.tender.status, Tender.Status.SUCCESS)
        self.assertEqual(push.tender.external_id, 9)
        self.assertEqual(push.tender.cargo_reference, 'CAR0009')
        self.assertTrue(push.tender.reference)
        self.assertEqual(
            push.tender.reference,
            Tender.make_reference(push.tender.pk, push.tender.created_at),
        )

    def test_flush_keeps_pending_when_still_down(self):
        push = self._enqueue()
        with patch('tenders.views.submit_tender', return_value=(None, '[Errno 111] Connection refused', False)):
            delivered, failed, skipped = tenders_views.flush_pending_pushes()
        self.assertEqual((delivered, failed, skipped), (0, 0, 0))
        push.refresh_from_db()
        self.assertEqual(push.state, self.pending_model.State.PENDING)
        self.assertEqual(push.attempts, 1)
        self.assertIn('connection refused', push.last_error)

    def test_flush_marks_4xx_permanently_failed(self):
        push = self._enqueue()
        with patch('tenders.views.submit_tender', return_value=(400, '{"message":"bad"}', False)):
            delivered, failed, skipped = tenders_views.flush_pending_pushes()
        self.assertEqual((delivered, failed, skipped), (0, 1, 0))
        push.refresh_from_db()
        self.assertEqual(push.state, self.pending_model.State.FAILED)

    def test_flush_skips_when_transporter_has_no_base_url(self):
        push = self._enqueue()
        self.odoo.base_url = ''
        self.odoo.save(update_fields=('base_url',))
        delivered, failed, skipped = tenders_views.flush_pending_pushes()
        self.assertEqual((delivered, failed, skipped), (0, 0, 1))
        push.refresh_from_db()
        self.assertEqual(push.state, self.pending_model.State.PENDING)
        self.assertEqual(self.pending_model.objects.filter(state=self.pending_model.State.PENDING).count(), 1)

    def test_successful_submission_flushes_queued(self):
        queued = self._enqueue()

        def send_tender(setting, tender):
            return (200, '{"status":"success","data":{"id":%d,"name":"CAR%04d"}}' % (tender.pk, tender.pk), True)

        with patch('tenders.views.submit_tender', side_effect=send_tender) as m:
            response = self._post_tender()
        self.assertTrue(response.json()['ok'])
        self.assertEqual(m.call_count, 2)
        queued.refresh_from_db()
        self.assertEqual(queued.state, self.pending_model.State.DELIVERED)

    def _second_transporter(self, name='Queue Transporter 2'):
        return OdooCompany.objects.create(
            name=name, base_url='https://queue2.example.com/', auth_type='bearer',
        )

    def test_partial_transient_failure_is_queued(self):
        """A tender accepted by one company but unreachable for another is still queued."""
        self._second_transporter()

        def send(setting, tender):
            if setting.pk == self.odoo.pk:
                return (200, '{"status":"success","data":{"id":1,"name":"CAR0001"}}', True)
            return (None, '[Errno 111] Connection refused', False)

        with patch('tenders.views.submit_tender', side_effect=send):
            response = self._post_tender()
        data = response.json()
        self.assertTrue(data['queued'])
        push = self.pending_model.objects.get()
        self.assertEqual(push.state, self.pending_model.State.PENDING)
        self.assertEqual(push.tender.submissions.filter(success=True).count(), 1)

    def test_flush_retries_only_undelivered_targets(self):
        second = self._second_transporter()

        def send_down(setting, tender):
            if setting.pk == self.odoo.pk:
                return (200, '{"status":"success","data":{"id":1,"name":"CAR0001"}}', True)
            return (None, '[Errno 111] Connection refused', False)

        with patch('tenders.views.submit_tender', side_effect=send_down):
            self._post_tender()
        push = self.pending_model.objects.get()

        calls = []

        def send_up(setting, tender):
            calls.append(setting.pk)
            return (200, '{"status":"success","data":{"id":2,"name":"CAR0002"}}', True)

        with patch('tenders.views.submit_tender', side_effect=send_up):
            delivered, failed, skipped = tenders_views.flush_pending_pushes()
        self.assertEqual((delivered, failed, skipped), (1, 0, 0))
        self.assertEqual(calls, [second.pk])
        push.refresh_from_db()
        self.assertEqual(push.state, self.pending_model.State.DELIVERED)

    def test_flush_stays_pending_until_every_target_delivered(self):
        second = self._second_transporter()
        third = self._second_transporter(name='Queue Transporter 3')
        with patch('tenders.views.submit_tender', return_value=(None, '[Errno 111] Connection refused', False)):
            self._post_tender()
        push = self.pending_model.objects.get()

        def send_partial(setting, tender):
            if setting.pk == self.odoo.pk:
                return (200, '{"status":"success","data":{"id":1,"name":"CAR0001"}}', True)
            return (None, '[Errno 111] Connection refused', False)

        with patch('tenders.views.submit_tender', side_effect=send_partial):
            delivered, failed, skipped = tenders_views.flush_pending_pushes()
        self.assertEqual((delivered, failed, skipped), (0, 0, 0))
        push.refresh_from_db()
        self.assertEqual(push.state, self.pending_model.State.PENDING)

        with patch('tenders.views.submit_tender', return_value=(200, '{"status":"success","data":{"id":2,"name":"CAR0002"}}', True)):
            delivered, failed, skipped = tenders_views.flush_pending_pushes()
        self.assertEqual((delivered, failed, skipped), (1, 0, 0))
        push.refresh_from_db()
        self.assertEqual(push.state, self.pending_model.State.DELIVERED)
        self.assertTrue(push.tender.submissions.filter(odoo_company=second, success=True).exists())
        self.assertTrue(push.tender.submissions.filter(odoo_company=third, success=True).exists())

    def test_permanently_rejected_target_is_not_retried(self):
        second = self._second_transporter()

        def send_mixed(setting, tender):
            if setting.pk == self.odoo.pk:
                return (400, '{"message":"bad"}', False)
            return (None, '[Errno 111] Connection refused', False)

        with patch('tenders.views.submit_tender', side_effect=send_mixed):
            self._post_tender()
        self.pending_model.objects.get()

        calls = []

        def send_up(setting, tender):
            calls.append(setting.pk)
            return (200, '{"status":"success","data":{"id":2,"name":"CAR0002"}}', True)

        with patch('tenders.views.submit_tender', side_effect=send_up):
            delivered, failed, skipped = tenders_views.flush_pending_pushes()
        self.assertEqual((delivered, failed, skipped), (1, 0, 0))
        self.assertEqual(calls, [second.pk])

    def test_admin_page_shows_pending_and_retries(self):
        self._enqueue()
        self.client.login(email='queueadmin@example.com', password='pass1234')
        response = self.client.get(reverse('tenders:admin_diagnostic'))
        self.assertContains(response, 'Pending &amp; failed tender submissions')
        with patch('tenders.views.submit_tender',
                   return_value=(200, '{"status":"success","data":{"id":10,"name":"CAR0010"}}', True)):
            response = self.client.post(reverse('tenders:admin_diagnostic'), {'action': 'force_retry'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.pending_model.objects.filter(state=self.pending_model.State.DELIVERED).count(), 1)
        self.assertEqual(self.pending_model.objects.filter(state=self.pending_model.State.PENDING).count(), 0)

    def test_admin_page_lists_failed_pushes(self):
        push = self._enqueue()
        push.state = self.pending_model.State.FAILED
        push.last_error = 'HTTP 400'
        push.save(update_fields=('state', 'last_error'))
        self.client.login(email='queueadmin@example.com', password='pass1234')
        response = self.client.get(reverse('tenders:admin_diagnostic'))
        self.assertContains(response, '1 failed')
        self.assertContains(response, 'HTTP 400')

    def test_admin_force_retry_reruns_failed_push(self):
        push = self._enqueue()
        push.state = self.pending_model.State.FAILED
        push.last_error = 'HTTP 400'
        push.save(update_fields=('state', 'last_error'))
        self.client.login(email='queueadmin@example.com', password='pass1234')
        with patch('tenders.views.submit_tender',
                   return_value=(200, '{"status":"success","data":{"id":11,"name":"CAR0011"}}', True)):
            response = self.client.post(reverse('tenders:admin_diagnostic'), {'action': 'force_retry'})
        self.assertEqual(response.status_code, 302)
        push.refresh_from_db()
        self.assertEqual(push.state, self.pending_model.State.DELIVERED)
        self.assertEqual(push.last_error, '')

    def test_admin_retry_single_push(self):
        first = self._enqueue()
        second = self._enqueue()
        self.client.login(email='queueadmin@example.com', password='pass1234')
        with patch('tenders.views.submit_tender',
                   return_value=(200, '{"status":"success","data":{"id":12,"name":"CAR0012"}}', True)):
            response = self.client.post(
                reverse('tenders:admin_diagnostic'),
                {'action': 'retry_push', 'push_id': first.pk},
            )
        self.assertEqual(response.status_code, 302)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.state, self.pending_model.State.DELIVERED)
        self.assertEqual(second.state, self.pending_model.State.PENDING)


class NavActiveStateTest(TestCase):
    """Only the matching nav link is highlighted (no substring false-positives)."""

    def setUp(self):
        self.user = CustomUser.objects.create_user(email='nav@example.com', password='pass1234')
        self.client.login(email='nav@example.com', password='pass1234')

    def _active(self, response, href, label):
        self.assertContains(response, f'<li class="active">\n          <a href="{href}">{label}</a>')

    def _not_active(self, response, href, label):
        self.assertNotContains(response, f'<li class="active">\n          <a href="{href}">{label}</a>')

    def test_tenders_active_not_orders(self):
        response = self.client.get(reverse('tenders:list'))
        self._active(response, reverse('tenders:list'), 'Tenders')
        self._not_active(response, reverse('tenders:order_list'), 'Orders')

    def test_orders_active_not_tenders(self):
        response = self.client.get(reverse('tenders:order_list'))
        self._active(response, reverse('tenders:order_list'), 'Orders')
        self._not_active(response, reverse('tenders:list'), 'Tenders')