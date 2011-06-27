from datetime import date

from django.test import TestCase

from .models import Person

class CompositeFieldTests(TestCase):

    def setUp(self):
        self.p1 = Person.objects.create(
            first_name='John', last_name='Lennon', birthday=date(1940, 10, 9)
        )
        self.p2 = Person.objects.create(
            first_name='George', last_name='Harrison', birthday=date(1943, 2, 25)
        )
        Person.objects.create(
            first_name='Paul', last_name='McCartney', birthday=date(1942, 6, 18)
        )
        Person.objects.create(
            first_name='Ringo', last_name='Starr', birthday=date(1940, 7, 7)
        )

    def test_cf_retrieval(self):
        name1 = self.p1.full_name
        self.assertEqual(name1.first_name, 'John')
        self.assertEqual(name1.last_name, self.p1.last_name)

        self.assertEqual(self.p2.full_name.first_name, self.p2.first_name)
        self.assertEqual(self.p2.full_name.last_name, 'Harrison')

    def test_cf_assignment(self):
        self.p1.full_name = ('Keith', 'Sanderson')
        self.assertEqual(self.p1.first_name, 'Keith')
        self.assertEqual(self.p1.last_name, 'Sanderson')

        name2 = self.p2.full_name._replace(first_name='Elliot',
                                           last_name='Roberts')
        self.p2.full_name = name2
        self.assertEqual(self.p2.first_name, name2.first_name)
        self.assertEqual(self.p2.last_name, name2.last_name)

    def test_cf_lookup(self):
        p1 = Person.objects.get(full_name=('John', 'Lennon'))
        self.assertEqual(p1.first_name, self.p1.first_name)
        self.assertEqual(p1.birthday, self.p1.birthday)

        qs = Person.objects.filter(full_name__in=[('John', 'Lennon'),
                                                  ('George', 'Harrison')])
        self.assertEqual(qs.count(), 2)
        self.assertQuerysetEqual(qs, [
            '<Person: John Lennon>',
            '<Person: George Harrison>'
        ])
