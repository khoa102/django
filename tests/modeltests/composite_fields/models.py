from __future__ import unicode_literals

from django.db import models
from django.utils.encoding import python_2_unicode_compatible


@python_2_unicode_compatible
class Person(models.Model):
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    birthday = models.DateField()

    full_name = models.CompositeField(first_name, last_name, primary_key=True)

    def __str__(self):
        return '%s %s' % (self.first_name, self.last_name)
