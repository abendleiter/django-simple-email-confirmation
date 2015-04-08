from __future__ import unicode_literals

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import models
from django.db.models.signals import post_init, post_save
from django.dispatch import receiver
from django.utils.crypto import get_random_string
from django.utils import timezone
from django.utils.translation import ugettext_lazy as _

from .exceptions import (
    EmailConfirmationExpired, EmailIsPrimary, EmailNotConfirmed,
)
from .signals import (
    email_confirmed, unconfirmed_email_created, primary_email_changed,
)


class SimpleEmailConfirmationUserMixin(object):
    """
    Mixin to be used with your django 1.5+ custom User model.
    Provides python-level functionality only.
    """

    # if your User object stores the User's primary email address
    # in a place other than User.email, you can override the
    # primary_email_field_name and/or primary_email get/set methods.
    # All access to a User's primary_email in this app passes through
    # these two get/set methods.

    primary_email_field_name = 'email'

    def get_primary_email(self):
        return getattr(self, self.primary_email_field_name)

    def __get_or_create_primary_address(self):
        """ Returns the EmailAddress matching the user's primary email,
            creating it if necessary; None if primary email field is blank.
        """
        email = getattr(self, self.primary_email_field_name)
        if email:
            address, created = self.email_address_set.get_or_create(email=email)
            return address
        return None

    def set_primary_email(self, email, require_confirmed=True):
        "Set an email address as primary"
        old_email = self.get_primary_email()
        if email == old_email:
            return

        if email not in self.get_confirmed_emails() and require_confirmed:
            raise EmailNotConfirmed()

        setattr(self, self.primary_email_field_name, email)
        self.save(update_fields=[self.primary_email_field_name])
        primary_email_changed.send(
            sender=self, old_email=old_email, new_email=email,
        )

    @property
    def is_confirmed(self):
        "Is the User's primary email address confirmed?"
        return self.get_primary_email() in self.get_confirmed_emails()

    @property
    def has_active_confirmation_request(self):
        "Is there an active confirmation request for the primary email address?"
        address = self.__get_or_create_primary_address()
        return bool(address and address.is_being_confirmed)

    @property
    def confirmed_at(self):
        "When the User's primary email address was confirmed, or None"
        address = self.__get_or_create_primary_address()
        return address and address.confirmed_at

    def get_confirmation_key(self, email=None):
        "Get the confirmation key for an email"
        if email:
            address = self.email_address_set.get(email=email)
        else:
            address = self.__get_or_create_primary_address()
            if not address:
                raise EmailAddress.DoesNotExist('User has no primary email')
        return address.key

    def get_confirmed_emails(self):
        "List of emails this User has confirmed"
        address_qs = self.email_address_set.filter(confirmed_at__isnull=False)
        return [address.email for address in address_qs]

    def get_unconfirmed_emails(self):
        "List of emails this User has been associated with but not confirmed"
        # Make sure primary email is registered:
        self.__get_or_create_primary_address()
        address_qs = self.email_address_set.filter(confirmed_at__isnull=True)
        return [address.email for address in address_qs]

    def confirm_email(self, confirmation_key, save=True):
        """
        Attempt to confirm an email using the given key.
        Returns the email that was confirmed, or raise an exception.
        """
        address = self.email_address_set.confirm(confirmation_key, save=save)
        return address.email

    def add_confirmed_email(self, email):
        "Adds an email to the user that's already in the confirmed state"
        # if email already exists, let exception be thrown
        address = self.email_address_set.create_confirmed(email)
        return address.key

    def add_unconfirmed_email(self, email):
        "Adds an unconfirmed email address and returns it's confirmation key"
        # if email already exists, let exception be thrown
        address = self.email_address_set.create_unconfirmed(email)
        return address.key

    def add_email_if_not_exists(self, email):
        """
        If the user already has the email, and it's confirmed, do nothing
        and return None.

        If the user already has the email, and it's unconfirmed, reset the
        confirmation. If the confirmation is unexpired, do nothing. Return
        the confirmation key of the email.
        """
        try:
            address = self.email_address_set.get(email=email)
        except EmailAddress.DoesNotExist:
            key = self.add_unconfirmed_email(email)
        else:
            if not address.is_confirmed:
                key = address.reset_confirmation()
            else:
                key = None

        return key

    def reset_email_confirmation(self, email):
        "Reset the expiration of an email confirmation"
        address = self.email_address_set.get(email=email)
        return address.reset_confirmation()

    def remove_email(self, email):
        "Remove an email address"
        # if email already exists, let exception be thrown
        if email == self.get_primary_email():
            raise EmailIsPrimary()
        address = self.email_address_set.get(email=email)
        address.delete()


class EmailAddressManager(models.Manager):

    def generate_key(self):
        "Generate a new random key and return it"
        # double django default length for ~142 bits of randomness
        return get_random_string(length=24)

    def create_confirmed(self, email, user=None):
        "Create an email address in the confirmed state"
        user = user or getattr(self, 'instance', None)
        if not user:
            raise ValueError('Must specify user or call from related manager')
        key = self.generate_key()
        now = timezone.now()
        # let email-already-exists exception propogate through
        address = self.create(
            user=user, email=email, key=key, set_at=now, confirmed_at=now,
        )
        return address

    def create_unconfirmed(self, email, user=None):
        "Create an email address in the unconfirmed state"
        user = user or getattr(self, 'instance', None)
        if not user:
            raise ValueError('Must specify user or call from related manager')
        key = self.generate_key()
        # let email-already-exists exception propogate through
        address = self.create(user=user, email=email, key=key)
        unconfirmed_email_created.send(sender=user, email=email)
        return address

    def confirm(self, key, user=None, save=True):
        "Confirm an email address. Returns the address that was confirmed."
        queryset = self.all()
        if user:
            queryset = queryset.filter(user=user)
        address = queryset.get(key=key)

        if address.is_key_expired:
            raise EmailConfirmationExpired()

        if not address.is_confirmed:
            address.confirmed_at = timezone.now()
            if save:
                address.save(update_fields=['confirmed_at'])
                email_confirmed.send(sender=address.user, email=address.email)

        return address


class EmailAddress(models.Model):
    "An email address belonging to a User"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name='email_address_set',
    )
    email = models.EmailField(max_length=255)
    key = models.CharField(max_length=40, unique=True)

    set_at = models.DateTimeField(
        default=timezone.now,
        help_text=_('When the confirmation key expiration was set'),
    )
    confirmed_at = models.DateTimeField(
        blank=True, null=True,
        help_text=_('First time this email was confirmed'),
    )
    requested_at = models.DateTimeField(
        blank=True, null=True,
        help_text=_('Last time confirmation was requested for this email'),
    )

    objects = EmailAddressManager()

    class Meta:
        unique_together = (('user', 'email'),)
        verbose_name_plural = "email addresses"

    def __str__(self):
        return '{} <{}>'.format(self.user, self.email)

    __unicode__ = __str__

    @property
    def is_confirmed(self):
        return self.confirmed_at is not None

    @property
    def is_primary(self):
        return bool(self.user.email == self.email)

    @property
    def is_being_confirmed(self):
        return self.requested_at is not None and not self.is_key_expired

    @property
    def key_expires_at(self):
        # By default, keys don't expire. If you want them to, set
        # settings.SIMPLE_EMAIL_CONFIRMATION_PERIOD to a timedelta.
        period = getattr(
            settings, 'SIMPLE_EMAIL_CONFIRMATION_PERIOD', None
        )
        return self.set_at + period if period is not None else None

    @property
    def is_key_expired(self):
        return self.key_expires_at and timezone.now() >= self.key_expires_at

    def reset_confirmation(self):
        """
        Re-generate the confirmation key and key expiration associated
        with this email.  Note that the previous confirmation key will
        cease to work.
        """
        self.key = self._default_manager.generate_key()
        self.set_at = timezone.now()

        self.confirmed_at = None
        self.requested_at = None
        self.save(update_fields=['key', 'set_at', 'confirmed_at', 'requested_at'])
        return self.key

    def set_requested(self, when=None):
        "Records that a confirmation request has been made"
        if when is None:
            when = timezone.now()
        self.requested_at = when
        self.save(update_fields=['requested_at'])


# by default, auto-add unconfirmed EmailAddress objects for new Users
if getattr(settings, 'SIMPLE_EMAIL_CONFIRMATION_AUTO_ADD', True):
    def auto_add(sender, **kwargs):
        if sender == get_user_model() and kwargs['created']:
            user = kwargs.get('instance')
            # softly failing on using these methods on `user` to support
            # not using the SimpleEmailConfirmationMixin in your User model
            # https://github.com/mfogel/django-simple-email-confirmation/pull/3
            if hasattr(user, 'get_primary_email'):
                email = user.get_primary_email()
            else:
                email = user.email
            if email:
                if hasattr(user, 'add_unconfirmed_email'):
                    user.add_unconfirmed_email(email)
                else:
                    user.email_address_set.create_unconfirmed(email)

    # TODO: try to only connect this to the User model. We can't use
    #       get_user_model() here - results in import loop.

    post_save.connect(auto_add)


@receiver(post_init, sender=EmailAddress)
def _auto_generate_confirmation_key(sender, instance, **kwargs):
    "Make sure EmailAddresses have a confirmation key after creation"
    if not instance.key:
        instance.key = instance._default_manager.generate_key()
