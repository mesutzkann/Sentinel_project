using System.Text.Json;
using System.Text.Json.Serialization;

namespace Sentinel.Samples.Common;

/// <summary>
/// The single JSON wire contract the sample services speak: snake_case names, no nulls, enums as
/// strings.
/// </summary>
/// <remarks>
/// It has to be shared rather than configured per service, because ASP.NET Core applies its
/// serializer options only to endpoints. An <see cref="HttpClient"/> call left on the defaults
/// writes camelCase, so <c>ProductName</c> goes out as <c>productName</c>, arrives at an endpoint
/// expecting <c>product_name</c>, and binds to null — a caller-side mistake that surfaces as a
/// not-null violation in the callee's database.
/// </remarks>
public static class SampleJson
{
    /// <summary>Pass this to every <c>PostAsJsonAsync</c> and <c>ReadFromJsonAsync</c> call.</summary>
    public static readonly JsonSerializerOptions Options = new(JsonSerializerDefaults.Web)
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
        Converters = { new JsonStringEnumConverter() },
    };

    /// <summary>
    /// Applies the same contract to an options instance owned by ASP.NET Core, which builds its
    /// own and cannot simply be handed <see cref="Options"/>.
    /// </summary>
    public static void Apply(JsonSerializerOptions target)
    {
        target.PropertyNamingPolicy = Options.PropertyNamingPolicy;
        target.DefaultIgnoreCondition = Options.DefaultIgnoreCondition;
        target.Converters.Add(new JsonStringEnumConverter());
    }
}
