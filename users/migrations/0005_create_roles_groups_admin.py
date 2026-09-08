from django.db import migrations


GROUPS = ('Administrator', 'Agents', 'Users')
ADMIN_EMAIL = 'leon.mangu@gmail.com'


def create_groups(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    for name in GROUPS:
        Group.objects.get_or_create(name=name)


def assign_administrator(apps, schema_editor):
    CustomUser = apps.get_model('users', 'CustomUser')
    Group = apps.get_model('auth', 'Group')
    try:
        user = CustomUser.objects.get(email=ADMIN_EMAIL)
    except CustomUser.DoesNotExist:
        return
    user.role = 'administrator'
    user.save(update_fields=('role',))
    group = Group.objects.get(name='Administrator')
    user.groups.add(group)


def remove_groups(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    Group.objects.filter(name__in=GROUPS).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0004_add_user_role'),
    ]

    operations = [
        migrations.RunPython(create_groups, remove_groups),
        migrations.RunPython(assign_administrator, migrations.RunPython.noop),
    ]