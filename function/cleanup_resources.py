import oci
import datetime

def get_idle_instances(compartment_id, threshold_hours):
    config = oci.config.from_file()
    compute_client = oci.core.ComputeClient(config)

    now = datetime.datetime.utcnow()
    idle_instances = []

    list_instances_response = compute_client.list_instances(compartment_id=compartment_id)
    for instance in list_instances_response.data:
        launch_time = instance.time_created
        elapsed_time = now - launch_time
        if elapsed_time.total_seconds() / 3600 > threshold_hours:
            idle_instances.append(instance)

    return idle_instances

def terminate_instance(instance_id):
    config = oci.config.from_file()
    compute_client = oci.core.ComputeClient(config)

    compute_client.terminate_instance(instance_id)

def handle_cleanup():
    compartment_id = '<YOUR_COMPARTMENT_OCID>'
    threshold_hours = 24

    idle_instances = get_idle_instances(compartment_id, threshold_hours)
    for instance in idle_instances:
        print(f"Terminating instance: {instance.display_name}")
        terminate_instance(instance.id)

handle_cleanup()
