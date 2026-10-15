import unittest

from service.app import PARAMETERS, ConfigError, build_command, normalize_config


def stress_payload() -> dict[str, object]:
    payload = {parameter["key"]: parameter["default"] for parameter in PARAMETERS}
    payload.update(
        {
            "mode": "stress",
            "kubeconfig": "",
            "image_url": "https://example.com/image.qcow2",
            "image_storage_class": "image-sc",
            "data_storage_class": "data-sc",
        }
    )
    return payload


class MultiDataVolumeConfigTest(unittest.TestCase):
    def test_default_count_is_one(self) -> None:
        config = normalize_config(stress_payload())

        self.assertEqual(config["data_volume_count"], 1)

    def test_count_is_passed_to_stress_script(self) -> None:
        payload = stress_payload()
        payload["data_volume_count"] = 15

        command = build_command(normalize_config(payload))
        flag_index = command.index("--data-volume-count")

        self.assertEqual(command[flag_index + 1], "15")

    def test_count_must_be_positive(self) -> None:
        payload = stress_payload()
        payload["data_volume_count"] = 0

        with self.assertRaisesRegex(ConfigError, "Data PVC count must be at least 1"):
            normalize_config(payload)


if __name__ == "__main__":
    unittest.main()
