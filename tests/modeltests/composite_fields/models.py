from __future__ import unicode_literals

from django.db import models
from django.utils.encoding import python_2_unicode_compatible, force_text


@python_2_unicode_compatible
class Person(models.Model):
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    birthday = models.DateField()

    full_name = models.CompositeField(first_name, last_name, primary_key=True)

    class Meta:
        ordering = ('last_name', 'first_name')

    def __str__(self):
        return '%s %s' % (self.first_name, self.last_name)


@python_2_unicode_compatible
class MostFieldTypes(models.Model):
    """
    This one is supposed to contain most of the various field types
    (except for all kinds of integer or char fields which are essentially
    the same for our needs).
    """

    bool_field = models.NullBooleanField()
    char_field = models.CharField(max_length=47)
    date_field = models.DateField()
    dtime_field = models.DateTimeField()
    time_field = models.TimeField()
    dec_field = models.DecimalField(max_digits=7, decimal_places=4)
    float_field = models.FloatField()
    int_field = models.IntegerField()

    # Now we put it all together.
    all_fields = models.CompositeField(bool_field, char_field, date_field,
                                       dtime_field, time_field, dec_field,
                                       float_field, int_field)

    class Meta:
        ordering = ('char_field',)

    def __str__(self):
        return 'char: %s; dtime: %r; int: %r' % (self.char_field,
                                                 self.dtime_field,
                                                 self.int_field)


@python_2_unicode_compatible
class EvenMoreFields(MostFieldTypes):
    extra_field = models.IntegerField()

    def __str__(self):
        super_text = force_text(super(EvenMoreFields, self))
        return '%s; extra: %r' % (super_text, self.extra_field)


class WeekDay(models.Model):
    pos = models.IntegerField(primary_key=True)
    name = models.CharField(max_length=10)


class Sentence(models.Model):
    sentence = models.CharField(max_length=128)


@python_2_unicode_compatible
class SentenceFreq(models.Model):
    weekday = models.ForeignKey(WeekDay, db_column='wd')
    sentence = models.ForeignKey(Sentence)
    score = models.FloatField()

    composite_key = models.CompositeField(
        weekday, sentence, primary_key=True)

    def __str__(self):
        return self.sentence.sentence.replace('?', self.weekday.name)
