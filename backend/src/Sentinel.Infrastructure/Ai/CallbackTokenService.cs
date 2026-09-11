using System.Security.Cryptography;
using System.Text;
using Microsoft.Extensions.Configuration;
using Sentinel.Application.Common;

namespace Sentinel.Infrastructure.Ai;

/// <summary>
/// The per-investigation callback token, as an HMAC rather than a stored secret.
/// </summary>
/// <remarks>
/// <para>
/// ADR-0002 asked for a single-use token per investigation. What ships is per-investigation and
/// not single-use, and the difference is worth being honest about: an investigation posts tens of
/// events with the same token, so "single use" could only ever have meant "for the life of one
/// run" — and it is not revoked when that run ends either, because the events the emitter
/// buffers and retries are delivered after the terminal one and are exactly the events worth
/// keeping.
/// </para>
/// <para>
/// What it does buy is containment. A token that leaks from one investigation cannot write
/// events into another, which a single shared secret — the original Phase 1 sketch — would have
/// allowed. Deriving it rather than storing it keeps that property without a table and without a
/// database round trip on every event.
/// </para>
/// </remarks>
public sealed class CallbackTokenService : ICallbackTokenService
{
    /// <summary>Preferred key. Falls back to the internal API key so a local stack needs no extra setup.</summary>
    public const string ConfigurationKey = "Internal:CallbackSecret";

    private readonly byte[] _key;

    public CallbackTokenService(IConfiguration configuration)
    {
        var secret = configuration[ConfigurationKey]
            ?? configuration["Internal:ApiKey"]
            ?? configuration["INTERNAL_API_KEY"];

        _key = string.IsNullOrWhiteSpace(secret) ? [] : Encoding.UTF8.GetBytes(secret);
    }

    public string Issue(Guid investigationId)
    {
        if (_key.Length == 0)
        {
            // The same refusal the internal token filter makes: an unset secret must not
            // quietly become an open callback surface.
            throw new InvalidOperationException(
                $"{ConfigurationKey} (or Internal:ApiKey) is not configured, so callbacks cannot be authenticated.");
        }

        return Convert.ToBase64String(Signature(investigationId));
    }

    public bool Verify(Guid investigationId, string? token)
    {
        if (_key.Length == 0 || string.IsNullOrWhiteSpace(token))
        {
            return false;
        }

        Span<byte> provided = stackalloc byte[32];

        if (!Convert.TryFromBase64String(token, provided, out var written) || written != provided.Length)
        {
            return false;
        }

        // Fixed-time, so a caller cannot learn the signature one byte at a time by measuring how
        // long the rejection took.
        return CryptographicOperations.FixedTimeEquals(provided, Signature(investigationId));
    }

    /// <summary>
    /// HMAC-SHA256 over the investigation id.
    /// </summary>
    /// <remarks>
    /// The id in its canonical text form rather than its bytes: the AI service never parses the
    /// token, but a future client in another language would have to reproduce this, and Guid
    /// byte order is the classic way for two implementations of the same scheme to disagree.
    /// </remarks>
    private byte[] Signature(Guid investigationId) =>
        HMACSHA256.HashData(_key, Encoding.UTF8.GetBytes(investigationId.ToString("D")));
}
