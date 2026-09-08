from datetime import timedelta
import json
from unittest.mock import patch

from django.contrib.gis.geos import Point
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from tenders import views as tenders_views
from tenders import selcom
from tenders.models import ApiSetting, Invoice, Order, OrderLine, Tender, Town
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
        self.assertEqual([o['order_name'] for o in data['orders']], ['ORD-9001', 'ORD-9000'])
        order = data['orders'][0]
        self.assertEqual(order['origin']['name'], 'Nairobi')
        self.assertEqual(order['destination']['name'], 'Mombasa')
        self.assertAlmostEqual(order['distance_km'], 480, delta=60)
        self.assertEqual(order['tender_ref'], self.tender.cargo_reference)
        self.assertGreaterEqual(len(order['trucks']), 1)
        for truck in order['trucks']:
            self.assertIn(truck['status'], ('En route', 'Delivered'))
            self.assertIn('lat', truck)
            self.assertIn('lng', truck)

    def test_api_tracker_skips_orders_without_awarded_lines(self):
        self._create_awarded_order(9002, 'ORD-9002')
        self._create_awarded_order(9003, 'ORD-9003', awarded=False)
        Order.objects.create(order_id=9004, order_name='ORD-9004', user=self.user)
        response = self.client.get(reverse('tenders:api_tracker'))
        self.assertEqual(response.status_code, 200)
        names = [o['order_name'] for o in response.json()['orders']]
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
        names = [o['order_name'] for o in response.json()['orders']]
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
        names = sorted(o['order_name'] for o in response.json()['orders'])
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
        by_name = {o['order_name']: o for o in response.json()['orders']}
        self.assertEqual(len(by_name['ORD-9005']['trucks']), 3)
        self.assertEqual(len(by_name['ORD-9006']['trucks']), 2)

    def test_api_tracker_returns_route_points(self):
        self._create_awarded_order(9007, 'ORD-9007')
        response = self.client.get(reverse('tenders:api_tracker'))
        order = response.json()['orders'][0]
        self.assertEqual(order['route'][0], [order['origin']['lat'], order['origin']['lng']])
        self.assertEqual(order['route'][-1], [order['destination']['lat'], order['destination']['lng']])
        self.assertGreaterEqual(order['distance_km'], 0)

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
    def setUp(self):
        from tenders.models import ApiSetting
        self.admin = CustomUser.objects.create_user(
            email='admin@example.com', password='pass1234', role=CustomUser.Role.ADMINISTRATOR,
        )
        self.user = CustomUser.objects.create_user(email='user@example.com', password='pass1234')
        self.setting = ApiSetting.objects.create(base_url='https://odo.example.com/')

    def test_non_admin_can_read_shared_setting(self):
        self.client.login(email='user@example.com', password='pass1234')
        response = self.client.get(reverse('tenders:api_settings_json'))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['setting']['base_url'], 'https://odo.example.com/')

    def test_non_admin_cannot_edit_shared_setting(self):
        self.client.login(email='user@example.com', password='pass1234')
        response = self.client.post(
            reverse('tenders:api_settings_json'),
            {'base_url': 'https://hacked.example.com/'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.setting.base_url, 'https://odo.example.com/')

    def test_admin_can_edit_shared_setting(self):
        self.client.login(email='admin@example.com', password='pass1234')
        response = self.client.post(
            reverse('tenders:api_settings_json'),
            {'base_url': 'https://admin.example.com/', 'auth_type': 'bearer', 'api_token': 'tok'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        self.setting.refresh_from_db()
        self.assertEqual(self.setting.base_url, 'https://admin.example.com/')
        self.assertEqual(self.setting.api_token, 'tok')

    def test_shared_setting_is_single_row(self):
        from tenders.models import ApiSetting
        before = ApiSetting.objects.count()
        self.client.login(email='user@example.com', password='pass1234')
        response = self.client.get(reverse('tenders:api_settings_json'))
        self.assertEqual(response.status_code, 200)
        self.client.get(reverse('tenders:api_settings_json'))
        self.assertEqual(ApiSetting.objects.count(), before)

    def test_settings_page_admin_only(self):
        self.client.login(email='user@example.com', password='pass1234')
        self.assertEqual(self.client.get(reverse('tenders:api_settings')).status_code, 403)
        self.client.login(email='admin@example.com', password='pass1234')
        self.assertEqual(self.client.get(reverse('tenders:api_settings')).status_code, 200)


class InvoicesPageTest(TestCase):
    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            email='invoice-admin@example.com', password='pass1234',
            role=CustomUser.Role.ADMINISTRATOR,
        )
        self.user_a = CustomUser.objects.create_user(email='invoice-a@example.com', password='pass1234')
        self.user_b = CustomUser.objects.create_user(email='invoice-b@example.com', password='pass1234')

    def _tender(self, user, ref):
        return Tender.objects.create(
            user=user, route_loading='Nairobi', route_delivery='Mombasa',
            customer=f'C-{ref}', cargo_type=Tender.CargoType.DRY_VAN,
            truck_type=Tender.TruckType.TRUCK, weight=10.0, number_of_trucks=1,
            distance_km=480, cargo_date=timezone.localdate(), cargo_reference=ref,
        )

    def _order(self, user, tender, order_id, ref):
        order = Order.objects.create(
            order_id=order_id, order_name=f'ORD-{order_id}', user=user, tender=tender,
            company_id=1, company_name='Alpha Haulage', cargo_reference=ref, state='confirmed',
            amount_total=0, currency='USD',
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
        ApiSetting.objects.create(base_url='https://odo.example.com/')
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
        ApiSetting.objects.create(base_url='https://odo.example.com/')
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
            'customer_name': '',
            'tax_id': 'TIN-123',
            'country': 'TZ',
        })
        invite.refresh_from_db()
        self.assertEqual(invite.status, 'paid')
        self.assertEqual(invite.number, 'INV/2026/00045')

    def test_admin_mark_paid_dict_data_format(self):
        ApiSetting.objects.create(base_url='https://odo.example.com/')
        invite = self._order(self.user_a, self._tender(self.user_a, 'REF-A'), 7016, 'REF-A')
        self.client.login(email='invoice-admin@example.com', password='pass1234')
        with patch('tenders.views.submit_confirmation',
                   return_value=(200, '{"status":"success","data":{"name":"EXT-INV-7016"}}', True)):
            response = self.client.post(reverse('tenders:api_invoice_paid', args=[invite.pk]), {})
        self.assertTrue(response.json()['ok'])
        invite.refresh_from_db()
        self.assertEqual(invite.number, 'EXT-INV-7016')

    def test_admin_mark_paid_without_external_name_keeps_local_number(self):
        ApiSetting.objects.create(base_url='https://odo.example.com/')
        invite = self._order(self.user_a, self._tender(self.user_a, 'REF-A'), 7015, 'REF-A')
        self.client.login(email='invoice-admin@example.com', password='pass1234')
        with patch('tenders.views.submit_confirmation', return_value=(200, '{"status":"success"}', True)):
            response = self.client.post(reverse('tenders:api_invoice_paid', args=[invite.pk]), {})
        self.assertTrue(response.json()['ok'])
        invite.refresh_from_db()
        self.assertEqual(invite.status, 'paid')
        self.assertEqual(invite.number, 'INV-7015')

    def test_admin_mark_paid_requires_external_success(self):
        ApiSetting.objects.create(base_url='https://odo.example.com/')
        invite = self._order(self.user_a, self._tender(self.user_a, 'REF-A'), 7008, 'REF-A')
        self.client.login(email='invoice-admin@example.com', password='pass1234')
        with patch('tenders.views.submit_confirmation', return_value=(500, 'boom', False)):
            response = self.client.post(reverse('tenders:api_invoice_paid', args=[invite.pk]), {})
        data = response.json()
        self.assertFalse(data['ok'])
        invite.refresh_from_db()
        self.assertEqual(invite.status, 'pending')

    def test_admin_mark_paid_without_setting_rejected(self):
        invite = self._order(self.user_a, self._tender(self.user_a, 'REF-A'), 7009, 'REF-A')
        self.client.login(email='invoice-admin@example.com', password='pass1234')
        response = self.client.post(reverse('tenders:api_invoice_paid', args=[invite.pk]), {})
        data = response.json()
        self.assertFalse(data['ok'])
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
        response = self.client.get(reverse('tenders:api_settings'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Selcom payment gateway')
        self.assertContains(response, 'webhook/selcom')