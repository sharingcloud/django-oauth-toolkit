from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('oauth2_provider', '0004_auto_20200902_2022'),
    ]

    operations = [
        migrations.AlterField(
            model_name='accesstoken',
            name='token',
            field=models.TextField(unique=True)
        ),
        migrations.AlterField(
            model_name='refreshtoken',
            name='token',
            field=models.TextField()
        )
    ]
