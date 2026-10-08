package io.mimicbrowser.sdk;

import com.google.gson.JsonObject;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

final class ContextConfigurationTest {
    @Test void typedConfigurationPreservesOmissionAndContextScope() {
        var settings = new ContextConfiguration();
        assertEquals(new JsonObject(), settings.toWire());
        var profile = new JsonObject();
        profile.add("generate", new JsonObject());
        settings.profile = Generated.OptionalValue.of(profile);
        settings.media = Generated.OptionalValue.of(null);
        var context = new MimicContext((method, parameters) -> {
            assertEquals("Mimic.configureContext", method);
            assertEquals("native-context", parameters.get("browserContextId").getAsString());
            assertEquals(profile, parameters.get("profile"));
            assertTrue(parameters.get("media").isJsonNull());
            assertFalse(parameters.has("proxy"));
            assertFalse(parameters.has("disposeOnDetach"));
            return new JsonObject();
        }, "native-context");
        context.configure(settings);
        assertFalse(profile.has("browserContextId"));
        assertFalse(settings.toWire().has("browserContextId"));
    }
}
