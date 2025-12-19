from django.db import models
from django.core.exceptions import ValidationError


# Create your models here.

class Product(models.Model):
    name = models.CharField(max_length=100)
    sku = models.CharField(max_length=50, unique=True)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    stock = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name
   #sale model to track sales transactions ####################################################################### 
class Sale(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    total_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0
    )

    def update_total(self):
        total = 0
        for item in self.items.all():
            total += item.price * item.quantity

        self.total_amount = total
        self.save()

    def __str__(self):
        return f"Sale #{self.id}"
####################################################################################################
class SaleItem(models.Model):
    sale = models.ForeignKey(
        Sale,
        related_name="items",
        on_delete=models.CASCADE
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT
    )
    quantity = models.PositiveIntegerField()
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2
    )

    def clean(self):
        # Check stock only when creating
        if self.pk is None:
            if self.quantity > self.product.stock:
                raise ValidationError(
                    f"Not enough stock for {self.product.name}"
                )

    def save(self, *args, **kwargs):
        self.clean()

        is_new = self.pk is None

        if is_new:
            self.product.stock -= self.quantity
            self.product.save()

        super().save(*args, **kwargs)

        self.sale.update_total()
        
    def delete(self, *args, **kwargs):
        print("DELETE METHOD CALLED ✅")

        self.product.stock += self.quantity
        self.product.save()

        sale = self.sale
        super().delete(*args, **kwargs)

        sale.update_total()


    def __str__(self):
        return f"{self.product.name} x {self.quantity}"

    ######################################################################
    
    
