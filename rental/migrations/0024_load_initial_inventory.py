from django.db import migrations


def load_initial_inventory(apps, schema_editor):
    Suit = apps.get_model('rental', 'Suit')

    items = [
        ("Black Classic Suit", "Standard", "50", "Black", 15, 5, "Available"),
        ("Navy Blue Suit", "Standard", "48", "Navy", 18, 3, "Available"),
        ("White Formal Shirt", "Standard", "L", "White", 5, 10, "Available"),
        ("Sky Blue Shirt", "Standard", "M", "Sky Blue", 5, 8, "Available"),
        ("Black Silk Tie", "Standard", "Standard", "Black", 3, 15, "Available"),
        ("Navy Silk Tie", "Standard", "Standard", "Navy", 3, 12, "Available"),
        ("Black Leather Shoes", "Standard", "43", "Black", 7, 6, "Available"),
        ("Brown Leather Shoes", "Standard", "42", "Brown", 7, 4, "Available"),
        ("Black Leather Belt", "Standard", "L", "Black", 2, 10, "Available"),
        ("Brown Leather Belt", "Standard", "M", "Brown", 2, 8, "Available"),
    ]

    for name, collection, size, color, price, qty, status in items:
        # Avoid creating duplicates based on name + size
        if not Suit.objects.filter(suit_name=name, size=size).exists():
            Suit.objects.create(
                suit_name=name,
                collection=collection,
                size=size,
                color=color,
                price_per_day=price,
                quantity=qty,
                status=status,
            )


def unload_initial_inventory(apps, schema_editor):
    Suit = apps.get_model('rental', 'Suit')
    names = [
        "Black Classic Suit",
        "Navy Blue Suit",
        "White Formal Shirt",
        "Sky Blue Shirt",
        "Black Silk Tie",
        "Navy Silk Tie",
        "Black Leather Shoes",
        "Brown Leather Shoes",
        "Black Leather Belt",
        "Brown Leather Belt",
    ]
    Suit.objects.filter(suit_name__in=names).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('rental', '0023_alter_suit_status'),
    ]

    operations = [
        migrations.RunPython(load_initial_inventory, reverse_code=unload_initial_inventory),
    ]
