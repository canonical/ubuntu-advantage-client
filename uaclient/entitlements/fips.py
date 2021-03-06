from itertools import groupby

from uaclient import apt
from uaclient.entitlements import repo
from uaclient.clouds.identity import get_cloud_type
from uaclient import status, util

try:
    from typing import Any, Callable, Dict, List, Set, Tuple, Union  # noqa

    StaticAffordance = Tuple[str, Callable[[], Any], bool]
except ImportError:
    # typing isn't available on trusty, so ignore its absence
    pass


class FIPSCommonEntitlement(repo.RepoEntitlement):

    repo_pin_priority = 1001
    repo_key_file = "ubuntu-advantage-fips.gpg"  # Same for fips & fips-updates

    """
    Dictionary of conditional packages to be installed when
    enabling FIPS services. For example, if we are enabling
    FIPS services in a machine that has openssh-client installed,
    we will perform two actions:

    1. Upgrade the package to the FIPS version
    2. Install the correspinding hmac version of that package.
    """
    conditional_packages = [
        "openssh-client",
        "openssh-client-hmac",
        "openssh-server",
        "openssh-server-hmac",
        "strongswan",
        "strongswan-hmac",
    ]

    # RELEASE_BLOCKER GH: #104, don't prompt for conf differences in FIPS
    # Review this fix to see if we want more general functionality for all
    # services. And security/CPC signoff on expected conf behavior.
    apt_noninteractive = True

    help_doc_url = "https://ubuntu.com/security/certifications#fips"
    _incompatible_services = ["livepatch"]

    def _prevent_fips_on_xenial_cloud_instances(self):
        cfg_allow_xenial_fips_on_cloud = util.is_config_value_true(
            config=self.cfg.cfg,
            path_to_value="features.allow_xenial_fips_on_cloud",
        )

        if cfg_allow_xenial_fips_on_cloud:
            return False

        series = util.get_platform_info()["series"]

        if series != "xenial":
            return False

        cloud_id = get_cloud_type()

        if cloud_id is None:
            return False

        return bool(cloud_id in ("azure", "gce"))

    @property
    def static_affordances(self) -> "Tuple[StaticAffordance, ...]":
        # Use a lambda so we can mock util.is_container in tests
        cloud_titles = {"azure": "an Azure", "gce": "a GCP"}
        cloud_id = get_cloud_type() or ""

        return (
            (
                "Cannot install {} on a container".format(self.title),
                lambda: util.is_container(),
                False,
            ),
            (
                status.MESSAGE_FIPS_BLOCK_ON_CLOUD.format(
                    service=self.title, cloud=cloud_titles.get(cloud_id)
                ),
                lambda: self._prevent_fips_on_xenial_cloud_instances(),
                False,
            ),
        )

    def _replace_metapackage_on_cloud_instance(
        self, packages: "List[str]"
    ) -> "List[str]":
        """
        Identify correct metapackage to be used if in a cloud instance.

        Currently, the contract backend is not delivering the right
        metapackage on a Bionic Azure or AWS cloud instance. For those
        clouds, we have cloud specific fips metapackages and we should
        use them. We are now performing that correction here, but this
        is a temporary fix.
        """
        cfg_disable_fips_metapackage_override = util.is_config_value_true(
            config=self.cfg.cfg,
            path_to_value="features.disable_fips_metapackage_override",
        )

        if cfg_disable_fips_metapackage_override:
            return packages

        series = util.get_platform_info().get("series")
        if series != "bionic":
            return packages

        cloud_id = get_cloud_type()
        if not cloud_id or cloud_id not in ("azure", "aws"):
            return packages

        cloud_metapkg = "ubuntu-{}-fips".format(cloud_id)
        # Replace only the ubuntu-fips meta package if exists
        return [
            cloud_metapkg if pkg == "ubuntu-fips" else pkg for pkg in packages
        ]

    @property
    def packages(self) -> "List[str]":
        packages = super().packages
        packages = self._replace_metapackage_on_cloud_instance(packages)
        installed_packages = apt.get_installed_packages()

        pkg_groups = groupby(
            self.conditional_packages,
            key=lambda pkg_name: pkg_name.replace("-hmac", ""),
        )

        for pkg_name, pkg_list in pkg_groups:
            if pkg_name in installed_packages:
                packages += pkg_list

        return packages

    def application_status(self) -> "Tuple[status.ApplicationStatus, str]":
        super_status, super_msg = super().application_status()
        if super_status != status.ApplicationStatus.ENABLED:
            return super_status, super_msg
        running_kernel = util.get_platform_info()["kernel"]
        if running_kernel.endswith("-fips"):
            return super_status, super_msg
        return (
            status.ApplicationStatus.ENABLED,
            "Reboot to FIPS kernel required",
        )

    def remove_packages(self) -> None:
        """Remove fips meta package to disable the service.

        FIPS meta-package will unset grub config options which will deactivate
        FIPS on any related packages.
        """
        installed_packages = set(apt.get_installed_packages())
        fips_metapackage = set(self.packages).difference(
            set(self.conditional_packages)
        )
        remove_packages = fips_metapackage.intersection(installed_packages)
        if remove_packages:
            env = {"DEBIAN_FRONTEND": "noninteractive"}
            apt_options = [
                '-o Dpkg::Options::="--force-confdef"',
                '-o Dpkg::Options::="--force-confold"',
            ]
            apt.run_apt_command(
                ["apt-get", "remove", "--assume-yes"]
                + apt_options
                + list(remove_packages),
                status.MESSAGE_ENABLED_FAILED_TMPL.format(title=self.title),
                env=env,
            )


class FIPSEntitlement(FIPSCommonEntitlement):

    name = "fips"
    title = "FIPS"
    description = "NIST-certified FIPS modules"
    origin = "UbuntuFIPS"

    fips_pro_package_holds = [
        "fips-initramfs",
        "libssl1.1",
        "libssl1.1-hmac",
        "libssl1.0.0",
        "libssl1.0.0-hmac",
        "libssl1.0.0",
        "libssl1.0.0-hmac",
        "linux-fips",
        "openssh-client",
        "openssh-client-hmac",
        "openssh-server",
        "openssh-server-hmac",
        "openssl",
        "strongswan",
        "strongswan-hmac",
    ]

    @property
    def static_affordances(self) -> "Tuple[StaticAffordance, ...]":
        static_affordances = super().static_affordances

        fips_update = FIPSUpdatesEntitlement(self.cfg)
        enabled_status = status.ApplicationStatus.ENABLED
        is_fips_update_enabled = bool(
            fips_update.application_status()[0] == enabled_status
        )

        return static_affordances + (
            (
                "Cannot enable {} when {} is enabled".format(
                    self.title, fips_update.title
                ),
                lambda: is_fips_update_enabled,
                False,
            ),
        )

    @property
    def messaging(
        self
    ) -> "Dict[str, List[Union[str, Tuple[Callable, Dict]]]]":
        return {
            "pre_enable": [
                (
                    util.prompt_for_confirmation,
                    {
                        "msg": status.PROMPT_FIPS_PRE_ENABLE,
                        "assume_yes": self.assume_yes,
                    },
                )
            ],
            "pre_disable": [
                (
                    util.prompt_for_confirmation,
                    {
                        "assume_yes": self.assume_yes,
                        "msg": status.PROMPT_FIPS_PRE_DISABLE,
                    },
                )
            ],
        }

    def setup_apt_config(self) -> None:
        """Setup apt config based on the resourceToken and directives.

        FIPS-specifically handle apt-mark unhold

        :raise UserFacingError: on failure to setup any aspect of this apt
           configuration
        """
        cmd = ["apt-mark", "showholds"]
        holds = apt.run_apt_command(cmd, " ".join(cmd) + " failed.")
        unholds = []
        for hold in holds.splitlines():
            if hold in self.fips_pro_package_holds:
                unholds.append(hold)
        if unholds:
            unhold_cmd = ["apt-mark", "unhold"] + unholds
            holds = apt.run_apt_command(
                unhold_cmd, " ".join(unhold_cmd) + " failed."
            )
        super().setup_apt_config()


class FIPSUpdatesEntitlement(FIPSCommonEntitlement):

    name = "fips-updates"
    title = "FIPS Updates"
    origin = "UbuntuFIPSUpdates"
    description = "Uncertified security updates to FIPS modules"

    @property
    def messaging(
        self
    ) -> "Dict[str, List[Union[str, Tuple[Callable, Dict]]]]":
        return {
            "pre_enable": [
                (
                    util.prompt_for_confirmation,
                    {
                        "msg": status.PROMPT_FIPS_UPDATES_PRE_ENABLE,
                        "assume_yes": self.assume_yes,
                    },
                )
            ],
            "pre_disable": [
                (
                    util.prompt_for_confirmation,
                    {
                        "assume_yes": self.assume_yes,
                        "msg": status.PROMPT_FIPS_PRE_DISABLE,
                    },
                )
            ],
        }
