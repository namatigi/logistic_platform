import django.contrib.gis.db.models.fields
import django.contrib.gis.geos
from django.contrib.gis.geos import Point
from django.db import migrations


def populate_points(apps, schema_editor):
    Town = apps.get_model('tenders', 'Town')
    for town in Town.objects.all():
        try:
            lng = float(town.longitude)
            lat = float(town.latitude)
        except (TypeError, ValueError):
            continue
        if lng or lat:
            town.point = Point(lng, lat, srid=4326)
            town.save(update_fields=('point',))


def revert_points(apps, schema_editor):
    Town = apps.get_model('tenders', 'Town')
    for town in Town.objects.filter(point__isnull=False):
        town.latitude = town.point.y
        town.longitude = town.point.x
        town.save(update_fields=('latitude', 'longitude'))


class Migration(migrations.Migration):

    dependencies = [
        ('tenders', '0008_town'),
    ]

    operations = [
        migrations.AddField(
            model_name='town',
            name='point',
            field=django.contrib.gis.db.models.fields.PointField(
                blank=True,
                help_text='Geographic location (longitude, latitude)',
                null=True,
                srid=4326,
                spatial_index=True,
            ),
        ),
        migrations.RunPython(populate_points, revert_points),
        migrations.RemoveField(model_name='town', name='latitude'),
        migrations.RemoveField(model_name='town', name='longitude'),
    ]