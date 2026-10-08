use mimic_sdk::generated::*;
use serde::{de::DeserializeOwned, Serialize};
use serde_json::{json, Value};

fn round_trip<T: DeserializeOwned + Serialize>(value: &Value) -> Value {
    serde_json::to_value(serde_json::from_value::<T>(value.clone()).unwrap()).unwrap()
}

#[test]
fn shared_wire_corpus() {
    let corpus: Value = serde_json::from_str(include_str!("fixtures/wire.json")).unwrap();
    for case in corpus["cases"].as_array().unwrap() {
        if !case["valid"].as_bool().unwrap() {
            continue;
        }
        let wire = &case["wire"];
        let actual = match case["model"].as_str().unwrap() {
            "CreateContextParams" => round_trip::<CreateContextParams>(wire),
            "ImportProfileParams" => round_trip::<ImportProfileParams>(wire),
            "GenerateProfileParams" => round_trip::<GenerateProfileParams>(wire),
            "GetProfileParams" => round_trip::<GetProfileParams>(wire),
            "ResourcePolicy" => round_trip::<ResourcePolicy>(wire),
            "UpdateResourcePolicyResult" => round_trip::<UpdateResourcePolicyResult>(wire),
            "SetMediaProfileParams" => round_trip::<SetMediaProfileParams>(wire),
            "MediaCameraRequest" => round_trip::<MediaCameraRequest>(wire),
            "GetMediaProfileResult" => round_trip::<GetMediaProfileResult>(wire),
            "GetTraceResult" => round_trip::<GetTraceResult>(wire),
            "TraceEvent" => round_trip::<TraceEvent>(wire),
            "CaptureSnapshotResult" => round_trip::<CaptureSnapshotResult>(wire),
            "ProtocolError" => round_trip::<ProtocolError>(wire),
            other => panic!("Missing shared fixture model: {}", other),
        };
        assert_eq!(*wire, actual, "{}", case["name"]);
    }
}

#[test]
fn null_false_and_absent_are_distinct() {
    let mut params = CreateContextParams::default();
    assert_eq!(serde_json::to_value(&params).unwrap(), json!({}));
    params.dispose_on_detach = WireOptional::Value(false);
    assert_eq!(
        serde_json::to_value(&params).unwrap(),
        json!({"disposeOnDetach":false})
    );
    params.dispose_on_detach = WireOptional::Null;
    assert_eq!(
        serde_json::to_value(&params).unwrap(),
        json!({"disposeOnDetach":null})
    );
    let restored: CreateContextParams =
        serde_json::from_value(json!({"disposeOnDetach":null})).unwrap();
    assert_eq!(restored.dispose_on_detach, WireOptional::Null);
}
