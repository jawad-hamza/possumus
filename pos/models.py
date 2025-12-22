from django.db import models
from django.core.exceptions import ValidationError
from decimal import Decimal, InvalidOperation


# Create your models here.
class Category(models.Model):
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name
    
#####################################################################
class Brand(models.Model):
    name = models.CharField(max_length=100, unique=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


## Supplier model to represent product suppliers ##################################################

class Supplier(models.Model):
    name = models.CharField(max_length=100)
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name

# Product model to represent items in the inventory ##################################################

class Product(models.Model):
    category = models.ForeignKey(
        Category,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="products"
    )
    
    brand = models.ForeignKey(
        Brand,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="products"
    )

    
    supplier = models.ForeignKey(
    Supplier,
    on_delete=models.SET_NULL,
    null=True,
    blank=True,
    related_name="products"
)
    name = models.CharField(max_length=100)
    sku = models.CharField(max_length=50, unique=True)
    barcode = models.CharField(max_length=50, unique=True, blank=True, null=True)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    stock = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name
##############################################################333   #############################
class Customer(models.Model):
    name = models.CharField(max_length=100)
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name
##################################################################################################

   #sale model to track sales transactions ####################################################################### 

class Sale(models.Model):
    customer = models.ForeignKey(
        Customer,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sales",
    )
    is_credit = models.BooleanField(
        default=False,
        help_text="Sale on loan / udhaar"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    discount_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    total_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0
    )

    def __str__(self):
        return f"Sale #{self.id}"

    def safe_decimal(self, value):
        try:
            return Decimal(value)
        except (TypeError, InvalidOperation):
            return Decimal("0")

    def update_total(self):
        subtotal = Decimal("0")
        for item in self.items.all():
            subtotal += item.price * item.quantity

        # clean values
        discount_rate = self.safe_decimal(self.discount_rate)
        if discount_rate < 0:
            discount_rate = Decimal("0")
        if discount_rate > 100:
            discount_rate = Decimal("100")

        tax_rate = self.safe_decimal(self.tax_rate)
        if tax_rate < 0:
            tax_rate = Decimal("0")
        if tax_rate > 100:
            tax_rate = Decimal("100")

        # compute
        discount_amount = subtotal * (discount_rate / Decimal("100"))
        after_discount = subtotal - discount_amount
        tax_amount = after_discount * (tax_rate / Decimal("100"))

        self.total_amount = after_discount + tax_amount
        self.save(update_fields=["total_amount"])
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

    ##################################################################################################
class Purchase(models.Model):
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="purchases"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    total_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0
    )

    def update_total(self):
        total = 0
        for item in self.items.all():
            total += item.cost_price * item.quantity
        self.total_amount = total
        self.save()

    def __str__(self):
        return f"Purchase #{self.id}"

##################################################################################################
class PurchaseItem(models.Model):
    purchase = models.ForeignKey(
        Purchase,
        related_name="items",
        on_delete=models.CASCADE
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT
    )
    quantity = models.PositiveIntegerField()
    cost_price = models.DecimalField(
        max_digits=10,
        decimal_places=2
    )

    def save(self, *args, **kwargs):
        is_new = self.pk is None

        if not is_new:
            old_item = PurchaseItem.objects.get(pk=self.pk)
            stock_change = self.quantity - old_item.quantity
        else:
            stock_change = self.quantity

        # Increase stock
        self.product.stock += stock_change
        self.product.save()

        super().save(*args, **kwargs)

        # Update purchase total
        self.purchase.update_total()

    def delete(self, *args, **kwargs):
        # Restore stock
        self.product.stock -= self.quantity
        self.product.save()

        purchase = self.purchase
        super().delete(*args, **kwargs)

        purchase.update_total()

    def __str__(self):
        return f"{self.product.name} x {self.quantity}"

#####`#############################################################################################`


from django.db import models
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType

class LedgerEntry(models.Model):
    DEBIT = 'debit'
    CREDIT = 'credit'

    ENTRY_TYPE_CHOICES = (
        (DEBIT, 'Debit'),
        (CREDIT, 'Credit'),
    )

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    party = GenericForeignKey('content_type', 'object_id')

    entry_type = models.CharField(
        max_length=6,
        choices=ENTRY_TYPE_CHOICES
    )

    amount = models.DecimalField(max_digits=12, decimal_places=2)

    reference = models.CharField(
        max_length=50,
        blank=True,
        help_text="Sale / Purchase / Payment / Loan"
    )

    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.party} | {self.entry_type} | {self.amount}"
    
########################
class CompanyProfile(models.Model):
    """
    Single source of truth for invoice branding & company info
    """

    name = models.CharField(max_length=200)
    logo = models.ImageField(
        upload_to="company_logos/",
        blank=True,
        null=True
    )

    address = models.TextField(blank=True)
    phone = models.CharField(max_length=50, blank=True)
    email = models.EmailField(blank=True)

    invoice_footer = models.TextField(
        blank=True,
        help_text="Text shown at bottom of invoice"
    )

    show_qr = models.BooleanField(default=True)
    qr_note = models.CharField(
        max_length=255,
        blank=True,
        help_text="Text below QR code"
    )

    is_active = models.BooleanField(
        default=True,
        help_text="Only one company profile should be active"
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Company Profile"
        verbose_name_plural = "Company Profile"

    def __str__(self):
        return self.name
    
    #######################################################################
    

    
    