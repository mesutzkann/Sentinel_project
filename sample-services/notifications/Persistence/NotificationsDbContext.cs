using Microsoft.EntityFrameworkCore;
using Sentinel.Samples.Notifications.Domain;

namespace Sentinel.Samples.Notifications.Persistence;

public sealed class NotificationsDbContext : DbContext
{
    public const string Schema = "svc_notifications";

    public NotificationsDbContext(DbContextOptions<NotificationsDbContext> options) : base(options)
    {
    }

    public DbSet<Notification> Notifications => Set<Notification>();

    protected override void OnModelCreating(ModelBuilder modelBuilder)
    {
        modelBuilder.HasDefaultSchema(Schema);

        modelBuilder.Entity<Notification>(entity =>
        {
            entity.ToTable("notifications");
            entity.HasKey(n => n.Id);

            entity.Property(n => n.Recipient).HasMaxLength(256).IsRequired();
            entity.Property(n => n.Subject).HasMaxLength(200).IsRequired();
            entity.Property(n => n.Body).HasMaxLength(2000).IsRequired();
            entity.Property(n => n.Channel).HasConversion<string>().HasMaxLength(16);
            entity.Property(n => n.Status).HasConversion<string>().HasMaxLength(16);
            entity.Property(n => n.FailureReason).HasMaxLength(500);

            entity.HasIndex(n => n.CreatedAt);
            entity.HasIndex(n => n.Status);
        });
    }
}
