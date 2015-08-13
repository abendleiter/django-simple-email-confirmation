# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import models, migrations


class Migration(migrations.Migration):

    dependencies = [
        ('simple_email_confirmation', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='emailaddress',
            name='requested_at',
            field=models.DateTimeField(null=True, blank=True, help_text='Last time confirmation was requested for this email'),
        ),
    ]
