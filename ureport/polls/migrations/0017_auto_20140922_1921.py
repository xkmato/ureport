# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import models, migrations


class Migration(migrations.Migration):

    dependencies = [
        ('polls', '0016_auto_20140922_1514'),
    ]

    operations = [
        migrations.AlterField(
            model_name='poll',
            name='category_image',
            field=models.ForeignKey(to='categories.CategoryImage', help_text='The splash category image to display for the poll (optional)', null=True),
        ),
    ]
