#!/usr/bin/env python3

import asyncio
import dataclasses
import functools
import logging
import os
import signal
import stat
import typing
import xml.etree.ElementTree as xml

import dbus_next as dbus
import evdev
import yaml


@dataclasses.dataclass
class VmOptions:
    hotkey: typing.FrozenSet[int] = dataclasses.field(default_factory=frozenset)


@dataclasses.dataclass
class VmConfig:
    devices: typing.Tuple[str] = dataclasses.field(default_factory=list)
    cpu: typing.Tuple[int] = dataclasses.field(default_factory=list)
    hugepages1G: int = 0
    hugepages2M: int = 0


class VfioKvmService(dbus.service.ServiceInterface):
    _DEFAULT_CONFIG_PATH = "/etc/vfio-kvm.yaml"
    _DEFAULT_QEMU_HOTKEY = ("KEY_LEFTCTRL", "KEY_RIGHTCTRL")
    _DEFAULT_HOTKEY = _DEFAULT_QEMU_HOTKEY

    _BUS_NAME = "vfio.kvm"
    _OBJ_PATH = "/vfio/kvm"

    async def __new__(cls, *args, **kwargs) -> dbus.service.ServiceInterface:
        instance = super().__new__(cls)
        await instance.__init__(*args, **kwargs)
        return instance

    async def __init__(self, config=None, bus=None, path=None) -> None:
        super().__init__(bus or self._BUS_NAME)
        self._vm_options = {}
        self._devices = {}
        self._targets = [None]
        self._target = None
        self._manage_cpu = False
        self._manage_hugepages = False
        self._parse_config(config=config or self._DEFAULT_CONFIG_PATH)
        await self._configure_dbus(bus or self._BUS_NAME, path or self._OBJ_PATH)
        logging.info("Listening for libvirtd events")

    def _parse_config(self, config):
        config = config or self._DEFAULT_CONFIG_PATH
        if not os.path.isfile(config):
            return
        with open(config) as fp:
            data = yaml.safe_load(fp) or {}
        if "host" in data:
            self._vm_options[None] = self._parse_vm_options(data["host"])
        self._vm_options.update(
            {
                key: self._parse_vm_options(value)
                for key, value in data.get("vm", {}).items()
            }
        )
        self._manage_cpu = data.get("manage_cpu", False)
        self._manage_hugepages = data.get("manage_hugepages", False)
        self._configure_hotkey(data.get("hotkey"))
        self._configure_qemu_hotkey(data.get("qemu_hotkey"))

    def _parse_vm_options(self, vm):
        return VmOptions(self._parse_hotkeys(vm.get("hotkey")))

    def _parse_hotkeys(self, hotkey):
        try:
            return frozenset(evdev.ecodes.ecodes[key] for key in hotkey)
        except:
            logging.warning(
                "Unable to match all keys in hotkey %s to integers. "
                "Hotkey will be unavailable.",
                hotkey,
            )
            return frozenset()

    def _configure_hotkey(self, keys):
        self._hotkey = self._parse_hotkeys(keys or self._DEFAULT_HOTKEY)

    def _configure_qemu_hotkey(self, keys):
        self._qemu_hotkey = self._parse_hotkeys(keys or self._DEFAULT_QEMU_HOTKEY)

    async def _configure_dbus(self, bus, path):
        _bus = await dbus.aio.MessageBus(
            bus_type=dbus.constants.BusType.SYSTEM
        ).connect()
        _bus.export(path, self)
        logging.debug("Requesting bus name %s", _bus.unique_name)
        await _bus.request_name(bus)
        logging.debug("Bus name %s granted", _bus.unique_name)

    def _parse_xml(self, xml_config: str) -> VmConfig:
        root = xml.fromstring(xml_config)
        cpu_pinnings = (
            int(e.get("cpuset")) for e in root.findall(".//cputune/vcpupin")
        )
        hugepages = root.find(".//memoryBacking/hugepages") is not None
        memory = int(root.findtext(".//memory"))
        mem_in_mb = memory // 1024
        gb_pages = mem_in_mb // 1024 if hugepages else 0
        mb_pages = mem_in_mb % gb_pages if hugepages else 0
        devices = (
            param[6:]
            for e in root.findall(
                ".//qemu:commandline/qemu:arg",
                {"qemu": "http://libvirt.org/schemas/domain/qemu/1.0"},
            )
            if "evdev=" in e.get("value")
            for param in e.get("value").split(",")
            if param.startswith("evdev=")
        )
        return VmConfig(devices, cpu_pinnings, gb_pages, mb_pages)

    @functools.cached_property
    def hotkey(self) -> frozenset:
        return frozenset(self._hotkey)

    @functools.cached_property
    def qemu_hotkey(self) -> frozenset:
        return frozenset(self._qemu_hotkey)

    def stop(self):
        for device in self._devices.values():
            device.stop()

    @dbus.service.dbus_property(name="Target")
    def target(self) -> "s":
        return self._target

    @target.setter
    def target(self, val: "s"):
        display = val or "host device"
        if val == self._target:
            logging.debug("%s selected but %s is already active", display, display)
            return
        logging.info("%s selected", display)
        self._target = val
        for device in self._devices.values():
            device.grab()
        self.emit_properties_changed({"Target": display})

    @dbus.service.method("Toggle")
    def toggle(self) -> "s":
        self.target = self._targets[
            (self._targets.index(self._target) + 1) % len(self._targets)
        ]
        return self.target

    @dbus.service.method("Prepare")
    def prepare(self, vm_name: "s", sub_op: "s", extra_op: "s", xml_config: "s") -> "b":
        logging.info("VM %s starting up", vm_name)
        logging.debug("libvirtd: %s %s %s\n%s", vm_name, sub_op, extra_op, xml_config)
        config = self._parse_xml(xml_config)
        self._targets.append(vm_name)
        self._pin_cpus(config.cpu)
        self._allocate_hugepages(config.hugepages1G, config.hugepages2M)
        self._create_devices(
            vm_name,
            config.devices,
            self._vm_options.get(None, VmOptions()).hotkey,
            self._vm_options.get(vm_name, VmOptions()).hotkey,
        )
        return True

    def _pin_cpus(self, cpu):
        if not self._manage_cpu or not cpu:
            return
        logging.info("Pinning CPUs: %s", ", ".join(str(c) for c in sorted(cpu)))

    def _allocate_hugepages(self, gb_pages, mb_pages):
        if not self._manage_hugepages or (not gb_pages and not mb_pages):
            return
        logging.info(
            "Allocating %d 1G hugepages and %d 2M hugepages", gb_pages, mb_pages
        )

    def _create_devices(self, vm_name, devices, host_hotkey=None, guest_hotkey=None):
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
            device = self._devices.get(source)
            device.add(vm_name, guest_hotkey)

    @dbus.service.method("Release")
    def release(self, vm_name: "s", sub_op: "s", extra_op: "s", xml_config: "s") -> "b":
        if vm_name not in self._targets:
            logging.debug("Attempted to release devices for unmanaged VM %s", vm_name)
            return False
        logging.info("VM %s shutting down", vm_name)
        logging.debug("libvirtd: %s %s %s\n%s", vm_name, sub_op, extra_op, xml_config)
        config = self._parse_xml(xml_config)
        self._targets.remove(vm_name)
        if self._target == vm_name:
            self.target = None
        self._destroy_devices(vm_name, config.devices)
        self._deallocate_hugepages(config.hugepages1G, config.hugepages2M)
        self._unpin_cpus(config.cpu)
        return True

    def _unpin_cpus(self, cpu):
        if not self._manage_cpu or not cpu:
            return
        logging.info("Unpinning CPUs: %s", ", ".join(str(c) for c in sorted(cpu)))

    def _deallocate_hugepages(self, gb_pages, mb_pages):
        if not self._manage_hugepages or (not gb_pages and not mb_pages):
            return
        logging.info(
            "Deallocating %d 1G hugepages and %d 2M hugepages", gb_pages, mb_pages
        )

    def _destroy_devices(self, vm_name, devices):
        is_last = len(self._targets) == 1
        for guest_source in devices:
            source = os.path.join(
                os.sep,
                "dev",
                "input",
                "by-id",
                os.path.basename(guest_source)[len(vm_name) + 1 :],
            )
            if source in self._devices:
                device = self._devices.pop(source)
                device.remove(vm_name)
            else:
                logging.warning(
                    "Attempted to destroy non-existent device %s", guest_source
                )


class ReplicatedDevice:
    def __init__(
        self,
        source: str,
        manager,
        host_hotkey: typing.Optional[typing.FrozenSet[int]] = None,
    ) -> None:
        if not os.path.exists(source) or not stat.S_ISCHR(os.stat(source).st_mode):
            raise IOError("No such device: %s", source)
        self._name = os.path.basename(source)
        self._source_path = source
        self._source = None
        self._manager = manager
        self._targets = {}
        self._hotkeys = {}
        self._grab_task = None
        self._replicate_task = None
        if host_hotkey:
            self._hotkeys[host_hotkey] = None

    def _get_device_path(self, target):
        return os.path.join(
            os.sep,
            "dev",
            "input",
            "by-id",
            f"{target}-{self._name}",
        )

    def _create_device(self, target, *, key=False):
        path = self._get_device_path(target)
        logging.info(f"Creating {target} device %s", path)
        device = evdev.UInput.from_device(self._source)
        self._targets[key if key is not False else target] = device
        if os.path.islink(path):
            logging.debug(f"Removing existing symlink %s", path)
            os.unlink(path)
        os.symlink(device.device, path)

    def _destroy_device(self, target, *, key=False):
        index = key if key is not False else target
        if index not in self._targets:
            return
        path = self._get_device_path(target)
        logging.info(f"Destroying {target} device %s", path)
        if os.path.islink(path):
            logging.debug(f"Removing symlink %s", path)
            os.unlink(path)
        self._targets.pop(index).close()

    async def _grab_source(self):
        while 1:
            try:
                self._source.grab()
                logging.debug("Grabbed source device %s", self._source.path)
            except IOError:
                pass
            except asyncio.CancelledError:
                return
            await asyncio.sleep(5)

    @property
    def _target(self) -> evdev.device.InputDevice:
        return self._targets.get(self._manager.target)

    async def _replicate(self) -> None:
        try:
            is_toggle = False
            hotkey_triggered = None
            async for event in self._source.async_read_loop():
                self._target.write_event(event)
                if event.type == evdev.ecodes.EV_KEY:
                    active_keys = frozenset(self._source.active_keys())
                    if event.value == 1 and active_keys == self._manager.hotkey:
                        is_toggle = True
                    elif is_toggle and not self._source.active_keys():
                        self._target.syn()  # Flush queued write events
                        await asyncio.sleep(0.1)  # Wait for events to flush
                        is_toggle = False
                        self._manager.toggle()
                    elif event.value == 1 and active_keys in self._hotkeys:
                        hotkey_triggered = active_keys
                    elif hotkey_triggered and not self._source.active_keys():
                        self._target.syn()  # Flush queued write events
                        await asyncio.sleep(0.1)  # Wait for events to flush
                        self._manager.target = self._hotkeys[hotkey_triggered]
                        hotkey_triggered = None
        except asyncio.CancelledError:
            return

    @property
    def _is_grabbed(self):
        try:
            self._target.device.grab()
        except IOError:
            return True
        self._target.device.ungrab()
        return False

    def grab(self):
        if not self._manager.target or self._is_grabbed:
            return
        logging.debug("Grabbing device %s", self._get_device_path(self._manager.target))
        for value in (1, 0):
            for key in self._manager.qemu_hotkey:
                self._target.write(evdev.ecodes.EV_KEY, key, value)
        self._target.syn()

    def start(self) -> None:
        if not self._source:
            self._source = evdev.InputDevice(self._source_path)
            self._create_device("host", key=None)
        if not self._grab_task:
            self._grab_task = asyncio.create_task(self._grab_source())
        if not self._replicate_task:
            self._replicate_task = asyncio.create_task(self._replicate())

    def stop(self) -> None:
        self._replicate_task.cancel()
        self._grab_task.cancel()
        for target in frozenset(self._targets.keys()):
            self._destroy_device(target if target else "host", key=target)
        try:
            self._source.ungrab()
            logging.info(f"Ungrabbed device %s", self._source.path)
            self._source.close()
            self._source = None
        except IOError:
            pass

    def add(self, vm_name: str, hotkey=None) -> None:
        if hotkey:
            self._hotkeys[hotkey] = vm_name
            logging.debug("Adding hotkey %s to VM %s", hotkey, vm_name)
        self.start()
        self._create_device(vm_name)

    def remove(self, vm_name: str) -> None:
        self._destroy_device(vm_name)
        if len(self._targets) == 1:
            self.stop()


async def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOGLEVEL", "INFO").upper(),
        format="[%(levelname)s] %(message)s",
    )
    manager = await VfioKvmService()

    def signal_handler() -> None:
        manager.stop()
        asyncio.get_event_loop().stop()

    for s in (signal.SIGINT, signal.SIGQUIT, signal.SIGTERM):
        asyncio.get_event_loop().add_signal_handler(s, signal_handler)


if __name__ == "__main__":
    task = asyncio.get_event_loop().create_task(main())
    try:
        asyncio.get_event_loop().run_forever()
    except SystemExit:
        task.exception()
        raise
