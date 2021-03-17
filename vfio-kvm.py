#!/usr/bin/env python3
"""
A service that reads libvirtd events from a hook and manages device creation and
deletion, replication, CPU pinning, and hugepages allocation.

Classes:
    DbusTypes: An enumeration of types used by dbus_next.
    ReplicatedDevice: Manages a real system device and creates virtual devices
        that can receive input only when a specific virtual machine (or the
        host) has focus.
    VmOptions: A dataclass containing options for virtual machines configured at
        launch and not in the XML configuration of the virtual machine.
    VmConfig: A class containing the relevant parsed sections of a virtual
        machine's XML configuration during the virtual machine's start.
    VfioKvmService: The service that manages hardware alterations and
        replications as well as signaling when a new virtual machine has focus.

Type Aliases:
    Hotkey: A structure describing a sequence of Linux-defined values
        representing key presses used to trigger an action.

Functions:
    handle_exception: Stops the event loop if a task has an exception.
    main: Called when this script is run as an executable. It creates the
        services and handles exceptions in the event loop.

Environment Variables:
    LOGLEVEL: The level of logs to output.
"""


from asyncio.tasks import Task
from typing import (
    Dict,
    FrozenSet,
    Iterable,
    Optional,
    Set,
    Tuple,
    Union,
    cast,
)
import asyncio
import dataclasses
import logging
import os
import pathlib
import signal
import stat
import xml.etree.ElementTree as xml

import dbus_next as dbus
import evdev
import yaml


Hotkey = FrozenSet[int]


class DbusTypes:
    """An enumeration to represent the strings used by dbus_next."""

    Boolean = "b"
    String = "s"


@dataclasses.dataclass
class VmOptions:
    """A dataclass to hold data virtual machine-specific options."""

    hotkey: Optional[Hotkey] = None


_EMPTY_VM_OPTIONS = VmOptions()


class VmConfig:
    """A representation of virtual-machine XML configuration values."""

    def __init__(self, xml_config: str):
        """Parse libvirt XML configuration.

        Parses an XML configuration for a virtual machine passed to the service
        via a libvirt hook. The relevant values are stored on this object for
        later reference.

        Args:
            xml_config: The XML configuration for a virtual machine that was
                passed to the service through a VM hook.
        """
        root = xml.fromstring(xml_config)
        name = root.findtext(".//name")
        hugepages = root.find(".//memoryBacking/hugepages") is not None
        memory = int(root.findtext(".//memory") or "0")
        mem_in_mb = memory // 1024
        mem_in_gb = mem_in_mb // 1024
        extra_memory = mem_in_mb % 2
        self.hugepages1G: int = mem_in_gb if hugepages else 0
        self.hugepages2M: int = (
            mem_in_mb % mem_in_gb / 2 + extra_memory if hugepages else 0
        )
        self.cpu: Tuple[int, ...] = tuple(
            int(e.get("cpuset", "0")) for e in root.findall(".//cputune/vcpupin")
        )
        self.devices: Set[str] = {
            dev
            for e in root.findall(".//devices/input[@type='passthrough']/source")
            if (dev := e.get("evdev", "")).startswith(f"/dev/input/by-id/{name}-")
        } | {
            param[6:]
            for e in root.findall(
                ".//qemu:commandline/qemu:arg",
                {"qemu": "http://libvirt.org/schemas/domain/qemu/1.0"},
            )
            if "evdev=" in (val := e.get("value", ""))
            for param in val.split(",")
            if param.startswith(f"evdev=/dev/input/by-id/{name}-")
        }


class VfioKvmService(dbus.service.ServiceInterface):
    """A D-BUS service that creates and manages virtual devices for libvirt
    virtual machines.

    D-Bus Methods:

        Toggle (string):
            This method cycles the currently active target to the next available
            machine.

            Returns: The newly activated target.

        Prepare (boolean):
            Accepts the information for a libvirt QEMU hook including the
            virtual machine name, the sub-operation, extra-operation and the XML
            configuration for the virtual machine. It then pins CPUs, allocates
            memory, and creates devices to use to send input to the virtual
            machine.

            Returns:
                A boolean value to indicate if it was successful or not. On a
                failure, the virtual machine will not be started.

        Release (boolean):
            Accepts the information for a libvirt QEMU hook including the
            virtual machine name, the sub-operation, extra-operation and the XML
            configuration for the virtual machine. If any resources were
            allocated for the virtual machine they will be freed up. Any devices
            created will be destroyed.

            Returns:
                A boolean value to indicate if it was successful or not. If it
                fails, there may be allocated resources that are not cleaned up.

    D-BUS Properties:

        Target (string):
            This property is emitted for any listeners whenever the target is
            changed. This allows listeners to act on changes to the focused
            target and take actions such as changing a monitor input.

    When the service is created, it reads settings from a given configuration
    file (default: /etc/vfio-kvm.yaml) if it exists. This file is used to read
    hotkeys for direct access to specific virtual machines, the host, or to
    change the QEMU hotkey and D-BUS bus name and object path. It can also set
    a hotkey to release the devices to the host without emitting a target change
    or altering the position in the virtual machine cycle.

    Valid options are:
        dbus_bus_name: The D-BUS bus name to request. This should be of the
            format:
                org.domain.subdomain
            The default is "vfio.kvm".
        dbus_object_path: The D-BUS path to export on the requested bus. It
            should be of the format:
                /org/domain/subdomain
            The default is "/vfio/kvm"
        manage_cpu: A boolean value to determine if the service should manage
            cputsets for pinned CPUs.
            The default is false.
        manage_hugepages: A boolean value to determine if the service should
            manage allocated hugepages.
            The default is false.
        hotkey: A list of key names of the form KEY_XXXX that when pressed
            together will cycle the active target to the next virtual machine.
            This defaults to the standard QEMU hotkey combination of
            KEY_LEFTCTRL and KEY_RIGHTCTRL.
            If this hotkey is not set, but qemu_hotkey is set
            this will use the value in qemu_hotkey.
        qemu_hotkey: A list of key names of the form KEY_XXXX used by QEMU to
            toggle between a host and a virtual machine. This is sent to virtual
            machines when they become active if they are not currently grabbing
            the virtual device.
            This defaults to the standard QEMU hotkey combination of
            KEY_LEFTCTRL and KEY_RIGHTCTRL.
        release_hotkey: A list of key names of the form KEY_XXXX that when
            pressed together will return control of the devices to the host
            machine until it is pressed again or the target is changed.
            There is no default value.
        host: A mapping of settings specific to the host device. Currently,
            only the "hotkey" setting is understood.
            hotkey: A list of key names of the form KEY_XXXX that when pressed
                together will set the active target to the host machine.
        vm: A mapping of virtual machine names to settings specific to that
            virtual machine. Each virtual machine supports the same options as
            the host setting.
    """

    _DEFAULT_CONFIG_PATH = pathlib.Path("/etc/vfio-kvm.yaml")
    _DEFAULT_QEMU_HOTKEY = ("KEY_LEFTCTRL", "KEY_RIGHTCTRL")

    _DEFAULT_BUS_NAME = "vfio.kvm"
    _DEFAULT_OBJ_PATH = "/vfio/kvm"

    async def __new__(cls, *args, **kwargs):
        """A workaround for async __init__ functions."""
        instance = super().__new__(cls)
        await instance.__init__(*args, **kwargs)
        return instance

    async def __init__(self, config: pathlib.Path = None) -> None:
        """Create a new D-BUS service for managing virtual machines.

        Requesting a bus name that the running user does not have access to will
        cause the program to hang indefinitely because dbus_next does not yield
        control or timeout.

        Args:
            config: A path to a configuration file containing hotkeys and other
                configuration options.
            bus: The bus name to request from D-BUS.
            path: The D-BUS path to export on the requested bus for this service.
        """
        self._released = False
        self._vm_options: Dict[Optional[str], VmOptions] = {}
        self._devices = {}
        self._targets = [None]  # None represents the host as a target
        self._target = None
        self._manage_cpu = False
        self._manage_hugepages = False
        self._configure(config)
        super().__init__(self._bus_name)
        await self._init_dbus(self._bus_name, self._dbus_path)
        logging.info("Listening for libvirtd events")

    def _configure(self, config: pathlib.Path = None) -> None:
        """Parse a YAML configuration file to configure this service.

        Args:
            config: A Path to a configuration file that contains settings for the
                service including hotkeys used for mapping keys to virtual machines.
        """
        config = config or self._DEFAULT_CONFIG_PATH
        if not config.is_file():
            return
        with open(config.resolve()) as fp:
            data = yaml.safe_load(fp) or {}
        if "host" in data:
            self._vm_options[None] = VmOptions(
                self._parse_hotkeys(data["host"].get("hotkey"))
            )
        self._vm_options.update(
            {
                key: VmOptions(self._parse_hotkeys(value.get("hotkey")))
                for key, value in data.get("vm", {}).items()
            }
        )
        self._bus_name = data.get("dbus_bus_name", self._DEFAULT_BUS_NAME)
        self._dbus_path = data.get("dbus_object_path", self._DEFAULT_OBJ_PATH)
        self._manage_cpu = data.get("manage_cpu", False)
        self._manage_hugepages = data.get("manage_hugepages", False)
        self._release_hotkey = self._parse_hotkeys(data.get("release_hotkey", []))
        qemu_hotkey = data.get("qemu_hotkey", self._DEFAULT_QEMU_HOTKEY)
        self._qemu_hotkey = self._parse_hotkeys(qemu_hotkey)
        self._hotkey = self._parse_hotkeys(data.get("hotkey", qemu_hotkey))

    def _parse_hotkeys(self, hotkey: Optional[Iterable[str]]) -> Optional[Hotkey]:
        """Convert a list of strings representing keys to a set of int codes.

        Args:
            hotkey: An iterable containing strings of the format KEY_XXX defined
                by the Linux kernel that can be converted to the integers
                returned by keyboard presses.

        Returns: A frozenset containing the integers represented by the strings
            in the initial iterable. If any of the strings is unable to be
            converted into an integer a warning will be logged and None will be
            returned instead of a frozenset.
        """
        try:
            return frozenset(evdev.ecodes.ecodes[key] for key in hotkey or ())
        except KeyError:
            logging.warning(
                "Unable to match all keys in hotkey %s to integers. "
                "Hotkey will be unavailable.",
                hotkey,
            )
            return None

    async def _init_dbus(self, bus: str, path: str) -> None:
        """Initialize D-BUS with the given bus name and path.

        Args:
            bus: The D-BUS bus name to request. This should be of the format:
                org.domain.subdomain
            path: The D-BUS path to export on the requested bus. It should be of
                the format: /org/domain/subdomain
        """
        _bus = await dbus.aio.MessageBus(
            bus_type=dbus.constants.BusType.SYSTEM
        ).connect()
        _bus.export(path, self)
        logging.debug("Requesting bus name %s", _bus.unique_name)
        await asyncio.wait_for(_bus.request_name(bus), timeout=30)
        logging.debug("Bus name %s granted", _bus.unique_name)

    @property
    def hotkey(self) -> Optional[Hotkey]:
        """Return the hotkey for toggling focus between virtual machines."""
        return self._hotkey

    @property
    def qemu_hotkey(self) -> Optional[Hotkey]:
        """Return the hotkey used by QEMU to toggle between a guest and host."""
        return self._qemu_hotkey

    @property
    def release_hotkey(self) -> Optional[Hotkey]:
        """Return the hotkey used to release devices to the host without
        changing the target."""
        return self._release_hotkey

    @property
    def released(self):
        """Return the device released state."""
        return self._released

    @released.setter
    def released(self, value: bool) -> bool:
        """Set the device released state to the given value.

        Returns the new device released state.
        """
        logging.debug(f"Released state set to {value}")
        self._released = value
        return self._released

    def stop(self) -> None:
        """Stop all devices running on the service."""
        for device in self._devices.values():
            device.stop()

    @dbus.service.dbus_property(name="Target")
    def target(self) -> DbusTypes.String:
        """Return the current target.

        This is a D-BUS property that can be queried for the currently active
        target as a string.
        """
        return self._target if not self._released else None

    @target.setter
    def target(self, val: DbusTypes.String):
        """Set the target to a specific virtual machine.

        This is a D-BUS property that can be used to change the currently active
        target. When the target is changed, any released devices are grabbed and
        a property change is emitted via D-BUS.

        If the target is set to the already active target, no change will be
        emitted and released devices will remain released.

        If the python value None is given the host device will be selected.
        """
        display = val or "host device"
        if val == self._target:
            logging.debug("%s selected but %s is already active", display, display)
            return
        logging.info("%s selected", display)
        self._released = False
        self._target = val
        for device in self._devices.values():
            device.grab()
        self.emit_properties_changed({"Target": display})

    @dbus.service.method("Toggle")
    def toggle(self) -> DbusTypes.String:
        """Cycle the active target to the next virtual machine.

        This is a D-BUS method that can be called to cycle the currently active
        virtual machine.
        """
        self.target = self._targets[
            (self._targets.index(self._target) + 1) % len(self._targets)
        ]
        return self.target

    @dbus.service.method("Prepare")
    def prepare(
        self,
        vm_name: DbusTypes.String,
        sub_op: DbusTypes.String,
        extra_op: DbusTypes.String,
        xml_config: DbusTypes.String,
    ) -> DbusTypes.Boolean:
        """Create devices to prepare for a new virtual machine.

        The service extracts information about requested passthrough devices,
        hugepages memory requests, and CPU tuning.

        The service creates new devices by removing "{vm_name}-" from the device
        and creating replicas of the base device to be used by the host and
        guest.

        If the "manage_cpu" option is enabled, it will set cpusets to restrict
        the kernel from adding processes to the pinned CPUs.

        If the "manage_hugepages" option is enabled and the virtual machine XML
        specifies "<hugepages/>" it will try to free up sufficient memory and
        dynamically allocate enough hugepages for the virtual machine.

        Args:
            vm_name: The name of the new virtual machine.
            sub_op: The libvirt sub-operation. Always "begin".
            extra_op: The libvirt extra-operation. Always "-".
            xml_config: The libvirt XML definition of the new virtual machine
                that is about to be started.
        """
        try:
            logging.info("VM %s preparing to start", vm_name)
            logging.debug(
                "libvirtd: %s %s %s\n%s", vm_name, sub_op, extra_op, xml_config
            )
            config = VmConfig(xml_config)
            self._targets.append(vm_name)
            self._pin_cpus(config.cpu)
            self._allocate_hugepages(config.hugepages1G, config.hugepages2M)
            self._create_devices(
                vm_name,
                config.devices,
                self._vm_options.get(None, _EMPTY_VM_OPTIONS).hotkey,
                self._vm_options.get(vm_name, _EMPTY_VM_OPTIONS).hotkey,
            )
            return True
        except Exception as e:
            logging.exception(
                "An exception occurred while preparing a virtual machine."
            )
            return False

    def _pin_cpus(self, cpu: Tuple[int, ...]) -> None:
        """Restrict kernel processes to pinned CPUs.

        If the "manage_cpu" option is enabled, it will set cpusets to restrict
        the kernel from adding processes to the pinned CPUs.

        Arg:
            cpu: A tuple of integers of CPUs to restrict. These should match up
                to pinned CPUs from the virtual machine XML configuration.
        """
        if not self._manage_cpu or not cpu:
            return
        logging.info("Pinning CPUs: %s", ", ".join(str(c) for c in sorted(cpu)))

    def _allocate_hugepages(self, gb_pages: int, mb_pages: int) -> None:
        """Allocate memory for hugepages.

        If the "manage_hugepages" option is enabled and the virtual machine XML
        specifies "<hugepages/>" it will try to free up sufficient memory and
        dynamically allocate enough hugepages for the virtual machine.

        Args:
            gb_pages: The number of 1GB hugepages to allocate. This should be
                calculated by dividing the memory requested for the virtual
                machine into 1GB chunks.
            mb_pages: The number of 2MB hugepages to allocate. This should be
                calculated by taking the remainder of the memory requested for
                the virtual machine after dividing it into 1GB chunks and then
                dividing that into 2MB chunks.
        """
        if not self._manage_hugepages or (not gb_pages and not mb_pages):
            return
        logging.info(
            "Allocating %d 1G hugepages and %d 2M hugepages", gb_pages, mb_pages
        )

    def _create_devices(
        self,
        vm_name: str,
        devices: Set[str],
        host_hotkey: Hotkey = None,
        guest_hotkey: Hotkey = None,
    ) -> None:
        """Create devices requested in the virtual machine's XML configuration.

        While parsing the XML configuration for the virtual machine all input
        devices starting with "/dev/input/by-id/{vm_name}-" are extracted from
        passthrough input tags and qemu:arg tags and passed to this function.

        For each device passed to this function, vm_name is removed from the
        device to get the true source device. For each source device, a
        ReplicatedDevice is created to divert input events from either the host
        or running guests.

        If a ReplicatedDevice already exists for the source device, the virtual
        machine will be added as an additional target for device.

        Args:
            vm_name: The name of the new virtual machine. This is used to
                determine the true source device from the requested guest source
                device given.
            devices: A tuple of strings representing devices that the virtual
                machine would like to have created. They are of the form:
                    /dev/input/by-id/{vm_name}-{device-ID}
                The vm_name is removed to give a source device of the form:
                    /dev/input/by-id/{device-ID}
            host_hotkey: A hotkey that the device should monitor to switch the
                target back to the host device.
            guest_hotkey: A hotkey that the device should monitor to switch the
                target to this specific virtual machine.
        """
        for guest_source in devices:
            source = os.path.join(
                os.sep,
                "dev",
                "input",
                "by-id",
                os.path.basename(guest_source)[len(vm_name) + 1 :],
            )
            if source not in self._devices:
                self._devices[source] = ReplicatedDevice(source, self, host_hotkey)
            device = self._devices[source]
            device.add(vm_name, guest_hotkey)

    @dbus.service.method("Release")
    def release(
        self,
        vm_name: DbusTypes.String,
        sub_op: DbusTypes.String,
        extra_op: DbusTypes.String,
        xml_config: DbusTypes.String,
    ) -> DbusTypes.Boolean:
        """Clean up any resources used by the stopped virtual machine.

        This is a D-BUS method that destroys any virtual devices created for the
        virtual machine. If this is the last virtual machine managed by the
        service, the source device will be released.

        If the "manage_cpu" option is enabled, it will set cpusets to remove CPU
        restrictions from any pinned CPUs the virtual machine was using.

        If the "manage_hugepages" option is enabled and the virtual machine XML
        specifies "<hugepages/>" any hugepages allocated for the virtual machine
        will be freed. If this is the last virtual machine managed by the
        service, hugepages and relevant features will be disabled.

        Args:
            vm_name: The name of the virtual machine that just shutdown.
            sub_op: The libvirt sub-operation. Always "end".
            extra_op: The libvirt extra-operation. Always "-".
            xml_config: The libvirt XML definition of the virtual machine
                that just shutdown.
        """
        try:
            if vm_name not in self._targets:
                logging.debug(
                    "Attempted to release devices for unmanaged VM %s", vm_name
                )
                return False
            logging.info("VM %s shutting down", vm_name)
            logging.debug(
                "libvirtd: %s %s %s\n%s", vm_name, sub_op, extra_op, xml_config
            )
            config = VmConfig(xml_config)
            self._targets.remove(vm_name)
            if self._target == vm_name:
                self.target = None
            self._destroy_devices(vm_name, config.devices)
            self._deallocate_hugepages(config.hugepages1G, config.hugepages2M)
            self._unpin_cpus(config.cpu)
            return True
        except Exception as e:
            logging.exception(
                "An exception occurred while preparing a virtual machine."
            )
            return False

    def _unpin_cpus(self, cpu: Tuple[int, ...]) -> None:
        """Remove process restrictions to CPUs used by the the virtual machine.

        If the "manage_cpu" option is enabled, it will set cpusets to allow
        the kernel to add processes to the CPUs that were pinned by the virtual
        machine.

        Arg:
            cpu: A tuple of integers of CPUs to allow. These should match up
                to pinned CPUs from the virtual machine XML configuration.
        """
        if not self._manage_cpu or not cpu:
            return
        logging.info("Unpinning CPUs: %s", ", ".join(str(c) for c in sorted(cpu)))

    def _deallocate_hugepages(self, gb_pages: int, mb_pages: int) -> None:
        """Deallocate memory used for hugepages by the virtual machine.

        If the "manage_hugepages" option is enabled and the virtual machine XML
        specifies "<hugepages/>" it will try to free any hugepages used by the
        virtual machine.

        Args:
            gb_pages: The number of 1GB hugepages to deallocate. This should be
                calculated by dividing the memory requested for the virtual
                machine into 1GB chunks.
            mb_pages: The number of 2MB hugepages to deallocate. This should be
                calculated by taking the remainder of the memory requested for
                the virtual machine after dividing it into 1GB chunks and then
                dividing that into 2MB chunks.
        """
        if not self._manage_hugepages or (not gb_pages and not mb_pages):
            return
        logging.info(
            "Deallocating %d 1G hugepages and %d 2M hugepages", gb_pages, mb_pages
        )

    def _destroy_devices(
        self, vm_name: str, devices: Set[str], guest_hotkey: Hotkey = None
    ) -> None:
        """Destroy devices created for use with the virtual machine.

        While parsing the XML configuration for the virtual machine all input
        devices starting with "/dev/input/by-id/{vm_name}-" are extracted from
        passthrough input tags and qemu:arg tags and passed to this function.

        All devices used by the virtual machine and given the prefix
        "{vm_name}-" will be destroyed.

        If this is the last virtual machine managed by this service, the source
        device will be freed and the ReplicatedDevice will be deleted.

        Args:
            vm_name: The name of the virtual machine. This is used to determine
                the true source device from the requested guest source device
                given.
            devices: A tuple of strings representing devices that the virtual
                machine had created that should be destroyed. They are of the
                form:
                    /dev/input/by-id/{vm_name}-{device-ID}
                The vm_name is removed to give a source device of the form:
                    /dev/input/by-id/{device-ID}
            guest_hotkey: A hotkey that the device used to monitor to switch the
                target to this specific virtual machine. This is necessary to
                remove it from the hotkeys monitored by the device.
        """
        is_last_vm = len(self._targets) == 1
        for guest_source in devices:
            source = os.path.join(
                os.sep,
                "dev",
                "input",
                "by-id",
                os.path.basename(guest_source)[len(vm_name) + 1 :],
            )
            device = self._devices[source]
            device.remove(vm_name, guest_hotkey)
            if is_last_vm:
                del self._devices[source]
                del device


class ReplicatedDevice:
    """XXX"""

    def __init__(
        self,
        source: str,
        manager: VfioKvmService,
        host_hotkey: Optional[Hotkey] = None,
    ) -> None:
        if not os.path.exists(source) or not stat.S_ISCHR(os.stat(source).st_mode):
            raise IOError("No such device: %s", source)
        self._name = os.path.basename(source)
        self._source_path = source
        self._source: Optional[evdev.InputDevice] = None
        self._manager = manager
        self._targets: Dict[Union[bool, None, str], evdev.InputDevice] = {}
        self._hotkeys: Dict[Hotkey, Optional[str]] = {}
        self._grab_task: Optional[Task] = None
        self._replicate_task: Optional[Task] = None
        if host_hotkey:
            self._hotkeys[host_hotkey] = None

    def _get_device_path(self, target: str) -> str:
        return os.path.join(
            os.sep,
            "dev",
            "input",
            "by-id",
            f"{target}-{self._name}",
        )

    def _create_device(
        self, target: str, *, key: Union[bool, None, str] = False
    ) -> None:
        path = self._get_device_path(target)
        logging.info(f"Creating {target} device %s", path)
        device = evdev.UInput.from_device(self._source)
        self._targets[key if key is not False else target] = device
        if os.path.islink(path):
            logging.debug(f"Removing existing symlink %s", path)
            os.unlink(path)
        os.symlink(device.device, path)

    def _destroy_device(
        self, target: str, *, key: Union[bool, None, str] = False
    ) -> None:
        index = key if key is not False else target
        if index not in self._targets:
            return
        path = self._get_device_path(target)
        logging.info(f"Destroying {target} device %s", path)
        if os.path.islink(path):
            logging.debug(f"Removing symlink %s", path)
            os.unlink(path)
        self._targets.pop(index).close()

    async def _grab_source(self) -> None:
        while 1:
            try:
                if self._source:
                    self._source.grab()
                    logging.debug("Grabbed source device %s", self._source.path)
            except IOError:
                pass
            except asyncio.CancelledError:
                return
            await asyncio.sleep(5)

    @property
    def _target(self) -> evdev.device.InputDevice:
        return self._targets[self._manager.target]

    async def _replicate(self) -> None:
        if not self._source:
            return

        source = self._source
        is_release = False
        is_toggle = False
        hotkey_triggered: Optional[Hotkey] = None

        async def handle_release(active_keys: Hotkey) -> None:
            nonlocal is_release
            if event.value == 1 and active_keys == self._manager.release_hotkey:
                is_release = True
            elif is_release and not source.active_keys():
                self._target.syn()
                await asyncio.sleep(0.1)
                is_release = False
                self._manager.released = not self._manager.released

        async def handle_toggle(active_keys: Hotkey) -> None:
            nonlocal is_toggle
            if event.value == 1 and active_keys == self._manager.hotkey:
                is_toggle = True
            elif is_toggle and not source.active_keys():
                self._target.syn()
                await asyncio.sleep(0.1)
                is_toggle = False
                self._manager.toggle()

        async def handle_hotkeys(active_keys: Hotkey) -> None:
            nonlocal hotkey_triggered
            if event.value == 1 and active_keys in self._hotkeys:
                hotkey_triggered = active_keys
            elif hotkey_triggered and not source.active_keys():
                self._target.syn()
                await asyncio.sleep(0.1)
                self._manager.target = self._hotkeys[hotkey_triggered]
                hotkey_triggered = None

        async for event in source.async_read_loop():
            self._target.write_event(event)
            if event.type == evdev.ecodes.EV_KEY:
                active_keys = frozenset(source.active_keys())
                await handle_release(active_keys)
                await handle_toggle(active_keys)
                await handle_hotkeys(active_keys)

    def grab(self) -> None:
        if not self._manager.target:
            return
        try:
            self._target.device.grab()
            self._target.device.ungrab()
        except IOError:
            return
        logging.debug("Grabbing device %s", self._get_device_path(self._manager.target))
        for value in (1, 0):
            for key in self._manager.qemu_hotkey or ():
                self._target.write(evdev.ecodes.EV_KEY, key, value)
        self._target.syn()

    def start(self) -> None:
        if not self._source:
            self._source = evdev.InputDevice(self._source_path)
            self._create_device("host", key=None)
        if not self._grab_task:
            self._grab_task = asyncio.create_task(
                self._grab_source(), name=f"Grab: {self._name}"
            )
            self._grab_task.add_done_callback(handle_exception)
        if not self._replicate_task:
            self._replicate_task = asyncio.create_task(
                self._replicate(), name=f"Replicate: {self._name}"
            )
            self._replicate_task.add_done_callback(handle_exception)

    def stop(self) -> None:
        if self._replicate_task:
            self._replicate_task.cancel()
        if self._grab_task:
            self._grab_task.cancel()
        for target in frozenset(self._targets.keys()):
            self._destroy_device(cast(str, target) if target else "host", key=target)
        try:
            if self._source:
                self._source.ungrab()
                logging.info(f"Ungrabbed device %s", self._source.path)
                self._source.close()
                self._source = None
        except IOError:
            pass

    def add(self, vm_name: str, hotkey: Optional[Hotkey] = None) -> None:
        if hotkey:
            self._hotkeys[hotkey] = vm_name
            logging.debug("Adding hotkey %s to VM %s", hotkey, vm_name)
        self.start()
        self._create_device(vm_name)

    def remove(self, vm_name: str, hotkey: Optional[Hotkey] = None) -> None:
        self._destroy_device(vm_name)
        if hotkey:
            self._hotkeys.pop(hotkey, None)
        if len(self._targets) == 1:
            self.stop()


def handle_exception(task: asyncio.Task) -> None:
    """Handle any exceptions that occur in tasks.

    Log all errors and stop the event loop when any exception other than
    asyncio.CancelledError is raised.

    Args:
        task: The task that raised the exception.
    """
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except:
        logging.exception("Exception raised by task %s", task.get_name())
        asyncio.get_event_loop().stop()


async def main() -> None:
    """Configure logging and error handling and start the service."""
    logging.basicConfig(
        level=os.environ.get("LOGLEVEL", "INFO").upper(),
        format="[%(levelname)s] %(message)s",
    )
    manager = await VfioKvmService()

    def signal_handler() -> None:
        """Stop the service and cleanup devices on receiving a signal."""
        manager.stop()
        asyncio.get_event_loop().stop()

    loop = asyncio.get_event_loop()

    for s in (signal.SIGINT, signal.SIGQUIT, signal.SIGTERM):
        loop.add_signal_handler(s, signal_handler)


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.create_task(main())
    loop.run_forever()
