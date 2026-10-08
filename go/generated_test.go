package mimic

import (
	"context"
	"encoding/json"
	"os"
	"reflect"
	"testing"
)

// These are semantic round trips through generated native model types. The
// same external fixture corpus is used by every language implementation.
func TestGeneratedWireCorpus(t *testing.T) {
	raw, err := os.ReadFile("../conformance/fixtures/wire.json")
	if err != nil {
		t.Fatal(err)
	}
	var corpus struct {
		Cases []struct {
			Name  string          `json:"name"`
			Model string          `json:"model"`
			Wire  json.RawMessage `json:"wire"`
			Valid bool            `json:"valid"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(raw, &corpus); err != nil {
		t.Fatal(err)
	}
	models := map[string]any{
		"CreateContextParams": &CreateContextParams{}, "ImportProfileParams": &ImportProfileParams{},
		"GenerateProfileParams": &GenerateProfileParams{}, "GetProfileParams": &GetProfileParams{},
		"ResourcePolicy": &ResourcePolicy{}, "UpdateResourcePolicyResult": &UpdateResourcePolicyResult{},
		"SetMediaProfileParams": &SetMediaProfileParams{}, "MediaCameraRequest": &MediaCameraRequest{},
		"GetMediaProfileResult": &GetMediaProfileResult{}, "GetTraceResult": &GetTraceResult{},
		"TraceEvent": &TraceEvent{}, "CaptureSnapshotResult": &CaptureSnapshotResult{},
		"ProtocolError": &ProtocolError{},
	}
	for _, test := range corpus.Cases {
		if !test.Valid {
			continue
		}
		t.Run(test.Name, func(t *testing.T) {
			prototype, ok := models[test.Model]
			if !ok {
				t.Fatalf("no model dispatch for shared case %s", test.Model)
			}
			value := reflect.New(reflect.TypeOf(prototype).Elem()).Interface()
			if err := json.Unmarshal(test.Wire, value); err != nil {
				t.Fatal(err)
			}
			encoded, err := json.Marshal(value)
			if err != nil {
				t.Fatal(err)
			}
			var want, got any
			_ = json.Unmarshal(test.Wire, &want)
			_ = json.Unmarshal(encoded, &got)
			if !reflect.DeepEqual(want, got) {
				t.Fatalf("wire changed\nwant %s\ngot  %s", test.Wire, encoded)
			}
		})
	}
}

func TestGeneratedOptionalNullFalseAndMissing(t *testing.T) {
	for _, test := range []struct {
		value CreateContextParams
		want  string
	}{
		{CreateContextParams{}, `{}`},
		{CreateContextParams{DisposeOnDetach: Some(false)}, `{"disposeOnDetach":false}`},
		{CreateContextParams{DisposeOnDetach: Null[bool]()}, `{"disposeOnDetach":null}`},
	} {
		encoded, err := json.Marshal(test.value)
		if err != nil || string(encoded) != test.want {
			t.Fatalf("encode %s %v; expected %s", encoded, err, test.want)
		}
		var restored CreateContextParams
		if err := json.Unmarshal(encoded, &restored); err != nil {
			t.Fatal(err)
		}
		if restored.DisposeOnDetach != test.value.DisposeOnDetach {
			t.Fatalf("optional state changed: %#v", restored)
		}
	}
}

type generatedRecordingSender struct {
	method string
	params []byte
}

func (s *generatedRecordingSender) Call(_ context.Context, method string, params, result any) error {
	s.method = method
	s.params, _ = json.Marshal(params)
	return json.Unmarshal([]byte(`{"profile":"opaque","profileId":"profile","mode":"generated","warnings":[],"browserContextId":"owned"}`), result)
}

func TestGeneratedDispatchKeepsCanonicalMethodAndInput(t *testing.T) {
	sender := &generatedRecordingSender{}
	client := MimicCommands{Sender: sender}
	got, err := client.CreateContext(context.Background(), CreateContextParams{DisposeOnDetach: Some(false)})
	if err != nil || sender.method != "Mimic.createContext" || string(sender.params) != `{"disposeOnDetach":false}` || got.BrowserContextId != "owned" {
		t.Fatalf("dispatch: %s %s %#v %v", sender.method, sender.params, got, err)
	}
}
