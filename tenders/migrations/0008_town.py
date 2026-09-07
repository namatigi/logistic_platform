from django.db import migrations, models


TOWN_COORDS = {
    'Tanzania': {
        'Dar es Salaam': (39.2083, -6.7924), 'Mwanza': (32.9175, -2.5164),
        'Arusha': (36.6830, -3.3869), 'Dodoma': (35.7516, -6.1630),
        'Mbeya': (33.4833, -8.9000), 'Zanzibar': (39.1921, -6.1659),
        'Tanga': (39.0986, -5.0694), 'Morogoro': (37.6591, -6.8235),
        'Tabora': (32.8986, -5.0167), 'Kigoma': (29.6333, -4.8833),
        'Mtwara': (40.1833, -10.2736), 'Musoma': (33.8000, -1.5000),
        'Iringa': (35.6914, -7.7700), 'Moshi': (37.3333, -3.3333),
        'Singida': (34.7500, -4.8167), 'Songea': (35.5750, -10.6833),
        'Babati': (35.8167, -3.8500), 'Sumbawanga': (31.6167, -7.9667),
        'Kahama': (32.5833, -2.8500), 'Shinyanga': (33.4167, -3.6667),
        'Bukoba': (31.3333, -1.3333), 'Njombe': (34.6500, -9.3333),
        'Geita': (32.1667, -2.8667), 'Mpanda': (31.0667, -6.3500),
        'Kibaha': (38.9333, -6.7667), 'Bagamoyo': (38.9000, -6.4333),
    },
    'Zambia': {
        'Lusaka': (28.3220, -15.3875), 'Kitwe': (28.2200, -12.8100),
        'Ndola': (28.6333, -12.9833), 'Livingstone': (25.8600, -17.8419),
        'Kabwe': (28.4473, -14.4469), 'Chipata': (33.6333, -13.6333),
        'Mufulira': (28.2500, -12.5500), 'Chingola': (27.8500, -12.5333),
        'Luanshya': (28.4000, -13.1333), 'Kasama': (31.1833, -10.2167),
        'Solwezi': (25.3833, -12.1333), 'Mongu': (23.1333, -15.2500),
        'Mansa': (29.1833, -11.2000), 'Choma': (25.9833, -16.8167),
        'Mazabuka': (27.7500, -15.8500), 'Kafue': (28.1833, -15.7667),
        'Nakonde': (33.0833, -9.3333), 'Mpika': (31.4167, -11.8333),
        'Kapiri Mposhi': (28.4500, -13.9667),
    },
    'Congo': {
        'Kinshasa': (15.3094, -4.4419), 'Lubumbashi': (27.5000, -11.6833),
        'Goma': (29.2229, -1.6784), 'Bukavu': (28.8594, -2.5089),
        'Kisangani': (25.1972, 0.5153), 'Kananga': (22.4167, -5.8833),
        'Mbuji-Mayi': (23.6000, -6.1500), 'Kolwezi': (25.4833, -10.7167),
        'Likasi': (26.7333, -10.9833), 'Matadi': (18.4333, -5.8167),
        'Kikwit': (18.6833, -3.8500), 'Uvira': (29.1167, -3.3964),
        'Beni': (29.4667, 0.4917), 'Kindu': (25.9500, -2.9500),
        'Mbandaka': (18.2611, 1.8333), 'Isiro': (27.6167, 2.7667),
        'Boma': (18.5500, -5.8500),
    },
    'Burundi': {
        'Bujumbura': (29.3599, -3.3731), 'Gitega': (29.9246, -3.4264),
        'Ngozi': (29.8225, -2.9075), 'Muyinga': (30.3333, -2.8500),
        'Rumonge': (29.4333, -3.9736), 'Ruyigi': (30.2500, -3.4750),
        'Cibitoke': (29.1264, -2.8864), 'Bururi': (29.6225, -3.9486),
        'Kayanza': (29.5717, -2.9222), 'Makamba': (29.8167, -4.1333),
        'Muramvya': (29.6083, -3.2633),
    },
    'Rwanda': {
        'Kigali': (29.8739, -1.9403), 'Huye': (29.5667, -2.5917),
        'Rubavu': (29.3333, -1.6833), 'Musanze': (29.6333, -1.5000),
        'Rusizi': (28.9167, -3.4917), 'Karongi': (29.4833, -2.0500),
        'Muhanga': (29.7833, -2.0833), 'Gicumbi': (29.6167, -1.5833),
        'Rwamagana': (30.4333, -1.9500), 'Nyagatare': (30.3167, -1.3000),
        'Bugesera': (30.1667, -2.2333),
    },
    'Kenya': {
        'Nairobi': (36.8219, -1.2921), 'Mombasa': (39.6682, -4.0435),
        'Kisumu': (34.7680, -0.1022), 'Nakuru': (36.0687, -0.3031),
        'Eldoret': (35.2698, 0.5143), 'Thika': (37.0693, -1.0488),
        'Malindi': (40.1169, -3.2192), 'Kitale': (35.0041, 1.0187),
        'Garissa': (39.6451, -0.4532), 'Nyeri': (36.9472, -0.4217),
        'Kakamega': (34.7525, 0.2828), 'Kericho': (35.2811, -0.3675),
        'Naivasha': (36.4316, -0.7167), 'Machakos': (37.2634, -1.5177),
        'Isiolo': (37.5833, 0.3500), 'Lodwar': (35.6000, 3.1167),
        'Mandera': (41.8667, 3.9333), 'Wajir': (40.0617, 1.7500),
    },
    'South Sudan': {
        'Juba': (31.5825, 4.8594), 'Wau': (24.5333, 7.7000),
        'Malakal': (31.6667, 9.5333), 'Bor': (31.5500, 6.2000),
        'Yei': (30.5167, 3.8667), 'Yambio': (29.4333, 4.7667),
        'Aweil': (27.4000, 8.7667), 'Rumbek': (29.6833, 6.8000),
        'Bentiu': (29.9167, 9.3333), 'Torit': (30.8333, 4.4167),
        'Kapoeta': (30.6500, 4.1167), 'Nimule': (31.5500, 3.6167),
        'Renk': (32.8000, 9.7167),
    },
    'Uganda': {
        'Kampala': (32.5825, 0.3476), 'Entebbe': (32.4639, 0.0561),
        'Gulu': (32.2975, 2.7747), 'Mbale': (34.1750, 1.0767),
        'Jinja': (33.2044, 0.4478), 'Mbarara': (30.6500, -0.6000),
        'Masaka': (31.7333, -0.3333), 'Lira': (32.9000, 2.2500),
        'Soroti': (33.6111, 1.7147), 'Arua': (30.9111, 2.9956),
        'Fort Portal': (30.2833, 0.6500), 'Kabale': (29.9833, -1.2486),
        'Mukono': (32.7539, 0.3475), 'Kasese': (30.0833, 0.1833),
        'Moroto': (34.3167, 2.5333), 'Tororo': (34.0833, 0.6833),
        'Busia': (34.1167, 0.4667), 'Kotido': (34.1000, 3.0500),
    },
}


def populate_towns(apps, schema_editor):
    Town = apps.get_model('tenders', 'Town')
    for country, towns in TOWN_COORDS.items():
        for town_name, (lng, lat) in towns.items():
            Town.objects.update_or_create(
                name=town_name,
                defaults={'country': country, 'longitude': lng, 'latitude': lat},
            )


def reverse_populate_towns(apps, schema_editor):
    Town = apps.get_model('tenders', 'Town')
    Town.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('tenders', '0007_order_transporter_alias'),
    ]

    operations = [
        migrations.CreateModel(
            name='Town',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=255, unique=True)),
                ('country', models.CharField(max_length=100)),
                (
                    'latitude',
                    models.DecimalField(decimal_places=6, default=0, help_text='Latitude in decimal degrees', max_digits=9),
                ),
                (
                    'longitude',
                    models.DecimalField(decimal_places=6, default=0, help_text='Longitude in decimal degrees', max_digits=9),
                ),
            ],
            options={
                'ordering': ['name'],
            },
        ),
        migrations.RunPython(populate_towns, reverse_populate_towns),
    ]